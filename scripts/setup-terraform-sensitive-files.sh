#!/bin/bash

# Get the workdir of the script
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"

echo $DIR

cd $DIR/../terraform

# Check if the terraform.tfvars file exists
if [ ! -f terraform.tfvars ]; then
    # If not, copy the empty file
    cp terraform.tfvars.empty terraform.tfvars
fi

# Check if the state.config file exists
if [ ! -f state.config ]; then
    # If not, copy the empty file
    cp state.config.empty state.config
fi