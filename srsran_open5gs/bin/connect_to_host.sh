#!/bin/bash
if [ -z "$1" ]; then
    echo "Usage: ./connect_to_host.sh <new_connection_name>"
    echo "Example: ./connect_to_host.sh remote-node"
    exit 1
fi


new_connection="$1"

remote_ip=$(grep -w "$new_connection" /etc/hosts | awk '{print $1}')

if [ -z "$remote_ip" ]; then
    echo "Could not find the IP address of ${new_connection} in /etc/hosts!"
    exit 1
fi


ovs_output=$(sudo ovs-vsctl show)

existing_connection=$(echo "$ovs_output" | grep -oP 'Port vxlan_\K\w+(?=_n3)')
existing_ip=$(grep -w "$existing_connection" /etc/hosts | awk '{print $1}')

if [ "$existing_connection" == "$new_connection" ]; then
    echo "Already connected to ${new_connection} (${remote_ip})!"
    exit 1
fi

if [ -n "$existing_connection" ]; then
    echo "There is an existing connection to ${existing_connection} (${existing_ip})!"
    read -p "Are you sure you want to proceed? This will delete the existing connection with ${existing_connection} and create a new connection with ${new_connection} [y/n]: " answer

    if [ "$answer" != "y" ]; then
        echo "Aborted. No changes were made."
        exit 1
    fi
fi

echo "Deleting existing connection to ${existing_connection} ..."
sudo ovs-vsctl del-port n3br vxlan_${existing_connection}_n3


echo "Proceeding with connecting to ${new_connection}..."
sudo ovs-vsctl add-port n3br vxlan_${new_connection}_n3 -- set Interface vxlan_${new_connection}_n3 type=vxlan options:remote_ip=${remote_ip} options:key=1003

if [ $? -eq 0 ]; then
    echo "Successfully connected to ${new_connection}: ${remote_ip}!"
else
    echo "Failed to connect to ${new_connection} (${remote_ip})!"
fi
