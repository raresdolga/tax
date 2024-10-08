#!/bin/bash
# MUST BE RUN FROM PROJECT ROOT DIR
ROOT_DIR=$PWD #/mnt/ # $PWD
echo "ROOT_DIR: ${ROOT_DIR}"
DATA_DIR=$ROOT_DIR/data/input/lra_data
echo "DATA_DIR: ${DATA_DIR}"
mkdir -p $DATA_DIR
# this might take some time
rm -rf $DATA_DIR/lra_release.gz  $DATA_DIR/lra_release  # Clean out any old datasets.

wget -v https://storage.googleapis.com/long-range-arena/lra_release.gz -P $DATA_DIR
# Add a progress bar because this can be slow.
pv $DATA_DIR/lra_release.gz | tar -zx -C $DATA_DIR

# Download the raw AAN data from the TutorialBank Corpus.
wget -v https://github.com/Yale-LILY/TutorialBank/blob/master/resources-v2022-clean.tsv -P $DATA_DIR

# unzip main lra without aan
tar -xvzf $DATA_DIR/lra_release.gz $DATA_DIR