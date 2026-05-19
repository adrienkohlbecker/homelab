# Cloudflare API tokens. The broad "homelab-tofu" account token tofu
# authenticates with is UI-managed: the v5 provider rejects every
# scope-touching cloudflare_account_token apply with "Provider produced
# inconsistent result after apply" because CF re-canonicalizes policy
# order on each PATCH and the post-apply consistency check refuses the
# rearrangement. This file manages only a narrow DNS-01 child for
# ansible's certbot role, so a host compromise gets DNS edit on the
# 3 zones and nothing else.
#
# Inspect the live homelab-tofu scopes when in doubt:
#   curl -sS -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
#     "https://api.cloudflare.com/client/v4/accounts/$ACCOUNT/tokens/1ce4167438950bf725420b2bf8a6dcec" \
#     | jq '.result.policies[] | {pg: [.permission_groups[].name], res: (.resources|keys)}'

locals {
  # Permission group UUIDs (verified 2026-05). Refresh via:
  #   curl ... "/accounts/$ACCOUNT/tokens/permission_groups" \
  #     | jq -r '.result[] | "\(.id)\t\(.name)"'
  cf_perm_groups = {
    "DNS Write" = "4755a26eedb94da69e1066d98aa820be"
    "Zone Read" = "c8fed203ed3043cba015a93ad1616f1f"
  }

  cf_zone_resources = {
    for zid in values(local.zones) : "com.cloudflare.api.account.zone.${zid}" => "*"
  }
}

# DNS:Write + Zone:Read on the 3 zones -- enough for certbot's DNS-01
# challenge (write _acme-challenge TXT, list zones to find the FQDN's
# parent), nothing else.
#
# Bootstrap / rotation (the latter via `-replace=`):
#   mise run tf -- apply [-replace=cloudflare_account_token.certbot]
#   mise run tf -- output -raw certbot_token \
#     | ansible-vault encrypt_string --encrypt-vault-id prod \
#         --stdin-name cloudflare_api_token
#   # replace cloudflare_api_token in group_vars/prod.yml with the envelope
#   mise run ansible --tags certbot
# The role's "Exercise renewal" task (certbot/tasks/main.yml:158)
# dry-runs the new credential end-to-end on apply.
resource "cloudflare_account_token" "certbot" {
  account_id = local.cloudflare_account_id
  name       = "certbot-dns01"
  status     = "active"

  policies = [
    {
      effect = "allow"
      permission_groups = [
        { id = local.cf_perm_groups["DNS Write"] },
        { id = local.cf_perm_groups["Zone Read"] },
      ]
      resources = jsonencode(local.cf_zone_resources)
    },
  ]
}

output "certbot_token" {
  value     = cloudflare_account_token.certbot.value
  sensitive = true
}
