#!/usr/bin/env bash
set -e
SERVER=root@95.38.163.249
rsync -az --delete --exclude .venv --exclude .env --exclude __pycache__ --exclude '*.db' --exclude .git ./ $SERVER:/opt/lead-finder/
ssh $SERVER 'cd /opt/lead-finder && chown -R leadfinder:leadfinder . && .venv/bin/pip install -q -r requirements.txt && systemctl restart leadfinder && systemctl is-active leadfinder'
