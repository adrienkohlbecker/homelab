# TOMBSTONE -- the `fox` VPS moved from Scaleway to Hetzner Cloud (see
# terraform/hetzner.tf). The Scaleway resources (instance, reserved IP,
# security group, iam_ssh_key) were deleted from config, but their entries
# still live in terraform state until the migration apply destroys them. A
# provider can't be dropped while it still manages state, so this empty
# provider block is kept alive for that one destroy.
#
# Auth still comes from the scw CLI config (~/.config/scw/config.yaml); `op
# run` leaves it untouched, so the mise `tf` wrapper picks it up.
#
# Cleanup once `tofu apply` has destroyed them (verify with
# `tofu state list | grep scaleway` returning nothing): delete this file and
# the `scaleway` entry in terraform/main.tf's required_providers.
provider "scaleway" {}
