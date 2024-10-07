"""Entry script to the LRA experiment"""

import os
import wandb
import logging
from typing import Tuple
from functools import partial
import jax
import optax
from datasets import disable_caching
from tax.examples.utils import parse_args
from tax.train.jax_single_host import Trainer as JaxTrainer, get_scheduler
from tax.examples.lra.model.task import Classification, Retreival
from tax.config import LRAConfig
from tax.evals.class_eval import ClassificEvaluator
from tax.examples.lra.lra_tok import (
    ListOpsTokenizer,
    ByteLevelTokenizer,
    ImageTokenizer,
)
from tax.examples.lra.lra_dp import (
    ListOpsDP,
    PathFinderDP,
    AANDP,
    IMBDDP,
    Cifrar10DP,
)

logging.basicConfig(
    format="%(asctime)s,%(msecs)d %(levelname)-8s [%(filename)s:%(lineno)d] %(message)s",
    datefmt="%Y-%m-%d:%H:%M:%S",
    level=logging.INFO,
)
LOG = logging.getLogger(__name__)


def map_nested_fn(fn):
    """
    Recursively apply `fn to the key-value pairs of a nested dict / pytree.
    We use this for some of the optax definitions below.
    """

    def map_fn(nested_dict):
        return {
            k: (map_fn(v) if hasattr(v, "keys") else fn(k, v))
            for k, v in nested_dict.items()
        }

    return map_fn


def prepare_optimizer(
    config: LRAConfig, total_steps: int
) -> Tuple[optax.GradientTransformation, optax.Schedule]:
    lr_scheduler = get_scheduler(config=config, total_steps=total_steps)

    ssm_fn = map_nested_fn(
        lambda k, _: (
            "ssm"
            if k
            in [
                "B",
                "Lambda_re",
                "Lambda_im",
                "log_step",
                "norm",
                "theta_log",
                "nu_log",
                "gamma_log",
                "B_re",
                "B_im",
            ]
            else "regular"
        )
    )

    regular_opt = optax.inject_hyperparams(optax.adamw)(
        learning_rate=lr_scheduler, weight_decay=config.weight_decay
    )

    optimizer = optax.multi_transform(
        {
            "ssm": optax.inject_hyperparams(optax.adam)(learning_rate=config.small_lr),
            "regular": regular_opt,
        },
        ssm_fn,
    )

    if config.grad_accumulation_steps > 1:
        optimizer = optax.MultiSteps(optimizer, config.grad_accumulation_steps)
    # chain with norm
    optimizer = optax.chain(optax.clip_by_global_norm(1.0), optimizer)
    return optimizer, lr_scheduler


def get_lra_dp(config: LRAConfig):
    dp, tokenizer = None, None
    cache_dir = os.path.join(config.base_dir, "input/lra_data/")
    if config.dataset_name == "listops":
        tokenizer = ListOpsTokenizer.from_pretrained(config.tokenizer_path)
        dp = ListOpsDP(tokenizer=tokenizer, cache_dir=cache_dir)
        raw_data = dp.get_raw_data()
        tok_data = dp.tokenize(raw_data, max_length=config.max_seq_len)
    elif config.dataset_name.startswith("pathfinder"):
        tokenizer = ImageTokenizer(vocab_size=None)
        dp = PathFinderDP(
            img_type=config.dataset_name,
            cache_dir=cache_dir,
            disable_cache=config.disable_cache,
            split="hard",
        )
        raw_data = dp.get_raw_data()
        tok_data = dp.tokenize(raw_data)
    elif config.dataset_name == "imdb":
        tokenizer = ByteLevelTokenizer(use_bos=False, use_eos=True)
        dp = IMBDDP(tokenizer, cache_dir)
        raw_data = dp.get_raw_data()
        tok_data = dp.tokenize(raw_data, max_length=config.max_seq_len)
    elif config.dataset_name == "aan":
        tokenizer = ByteLevelTokenizer(use_bos=False, use_eos=True)
        dp = AANDP(tokenizer=tokenizer, cache_dir=cache_dir)
        raw_data = dp.get_raw_data()
        tok_data = dp.tokenize(raw_data, max_length=config.max_seq_len)
    elif config.dataset_name == "cifar10":
        if config.tokenize_img:
            tokenizer = ImageTokenizer(vocab_size=256)
        else:
            tokenizer = ImageTokenizer()
        dp = Cifrar10DP(
            cache_dir=cache_dir, normalize=config.normalize_img, tokenizer=tokenizer
        )
        LOG.info("The data processor is %s ", dp)
        raw_data = dp.get_raw_data()
        tok_data = dp.tokenize(raw_data)
    else:
        raise ValueError("Unrecognised dataset name")
    return dp, tokenizer, raw_data, tok_data


class LRATask:
    def __init__(self, config: LRAConfig) -> None:
        LOG.info("Config is %s", config)
        self.config = config
        self.report_to = "none"
        self.wandb_run = None

        self.out_dir = os.path.join(
            self.config.base_dir, "out_latte/lra", self.config.name
        )
        os.makedirs(self.out_dir, exist_ok=True)
        self.set_logger()

        self.dp, self.tokenizer, self.raw_data, self.tokenized_data = get_lra_dp(config)
        self.data_collator = self.dp.get_collate_fn(return_type="np")
        print(self.raw_data)
        if config.dataset_name == "aan":
            self.model = Retreival(
                config,
                vocab_size=self.tokenizer.vocab_size,
                pad_id=self.tokenizer.pad_token_id,
            )
        else:
            self.model = Classification(
                config,
                vocab_size=self.tokenizer.vocab_size,
                pad_id=self.tokenizer.pad_token_id,
            )

    def set_logger(self):
        # configure wandb logs
        if self.config.wandb_log:
            resume = False
            if not self.config.check_path is None:
                resume = True
            wandb_run = wandb.init(
                project=self.config.project,
                entity=self.config.entity,
                name=self.config.name,
                dir=self.out_dir,
                config=self.config,
                resume=resume,
            )
            self.report_to = "wandb"
            self.wandb_run = wandb_run

    def train(self, train_rng):
        train_rng, init_rng = jax.random.split(train_rng, 2)
        evaluator = ClassificEvaluator(
            self.tokenized_data["validation"],
            data_collator=self.data_collator,
            config=self.config,
        )
        test_evaluator = ClassificEvaluator(
            self.tokenized_data["test"],
            data_collator=self.data_collator,
            config=self.config,
        )
        trainer = JaxTrainer(
            config=self.config,
            out_dir=self.out_dir,
            model=self.model,
            train_data=self.tokenized_data["train"],
            train_dl=None,
            evaluator=evaluator,
            test_evaluator=test_evaluator,
            data_collator=self.data_collator,
            wandb_run=self.wandb_run,
            rng=init_rng,
            model_inputs_orded=("input_ids", "labels"),
            prepare_opt_fn=prepare_optimizer,
        )
        if not self.config.check_path is None:
            trainer.train(train_rng, self.config.check_path)
        else:
            trainer.train(train_rng)


def main():
    seed = 0
    rng = jax.random.PRNGKey(seed)
    rng, train_rng = jax.random.split(rng)
    args = parse_args()
    config = LRAConfig.load(
        yaml_file=args.config_file, base_dir=args.base_dir, name=args.name
    )

    if config.disable_cache:
        LOG.info("Disabling Cache")
        disable_caching()

    task = LRATask(config)
    task.train(train_rng)


if __name__ == "__main__":
    main()
