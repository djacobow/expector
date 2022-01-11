#!/bin/bash

COUNTER=1
while [ "1" ];
do
    echo "$COUNTER"
    COUNTER=$[$COUNTER + 1]
    sleep ${1} 
done

