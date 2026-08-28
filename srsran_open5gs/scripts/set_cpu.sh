#!/bin/bash

POD_NAME=$(kubectl get pods -n open5gs | grep $1 | awk '{print $1}')

CPU=$2

echo "Setting CPU to $CPU for pod $POD_NAME"

kubectl patch -n open5gs pod $POD_NAME --type='json' -p='[{"op": "replace", "path": "/spec/containers/0/resources/requests/cpu", "value": "'$CPU'm"},{"op": "replace", "path": "/spec/containers/0/resources/limits/cpu", "value": "'$CPU'm"}]'
