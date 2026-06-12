# DECOMMISSIONED: the dedicated "homelab-ci" Hetzner Cloud project hosted the
# ephemeral CI worker fleet (notes/ci_ephemeral_hetzner_workers.md), retired
# for the AWS test cells (notes/ci_aws_test_cells.md gate D). The ci_worker +
# ci_builder firewalls were deleted from this file; the variable + aliased
# provider below must outlive them by one apply — destroying a resource needs
# its provider config still present.
#
# Teardown sequence:
#   1. `mise run tf plan` (2 destroys) -> apply.
#   2. Cloud Console: delete the homelab-ci project (revokes its token);
#      archive the 1Password item (op://Lab/buz7a2s327niiv2bet74apjreq).
#   3. Remove this file plus mise.toml's TF_VAR_hcloud_ci_token entry
#      (op run fails on refs to archived items, which would block
#      `mise run tf`).
variable "hcloud_ci_token" {
  description = "Read/write API token for the dedicated homelab-ci Hetzner project (decommissioned; see teardown sequence above)."
  type        = string
  sensitive   = true
}

provider "hcloud" {
  alias = "ci"
  token = var.hcloud_ci_token
}
