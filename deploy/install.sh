#!/bin/sh
set -eu

install -m 0644 deploy/goldman-podcasts.service /etc/systemd/system/goldman-podcasts.service
install -m 0644 deploy/goldman-podcasts.timer /etc/systemd/system/goldman-podcasts.timer
systemctl daemon-reload
systemctl enable --now goldman-podcasts.timer
