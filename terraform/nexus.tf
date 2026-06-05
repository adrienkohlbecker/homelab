provider "nexus" {
  url = "https://nexus.lab.fahm.fr"
  # NEXUS_USERNAME / NEXUS_PASSWORD come from terraform/.env via `op run`.
}

resource "nexus_blobstore_file" "default" {
  name = "default"
  path = "default"

  # Banner-only warning when usage crosses this threshold; doesn't block
  # writes. Sized for the /mnt/scratch zvol the store lives on —
  # adjust if that zvol's quota changes. spaceUsedQuota is "alert when
  # used data exceeds limit"; the alternative spaceRemainingQuota is
  # "alert when free space drops below limit".
  soft_quota {
    type  = "spaceUsedQuota"
    limit = 100 * 1024 * 1024 * 1024 # 100 GiB
  }
}

# Snapshotted blob store for the hosted (push-built) docker repos. A relative
# path resolves under the container's /nexus-data/blobs/, i.e.
# /mnt/services/nexus/data/blobs/hosted on the snapshotted services dataset --
# unlike "default", whose /nexus-data/blobs/default is bind-mounted to the
# non-snapshotted /mnt/scratch (see roles/nexus/templates/nexus.service.j2).
# Keeping hosted artifacts on the same snapshotted dataset as the Nexus DB
# means a snapshot restore brings repo metadata + blobs back consistently;
# scratch-backed proxy caches just re-proxy cold, but hosted images can't be
# re-proxied, so a scratch loss would otherwise strand the DB with dangling
# blob references.
resource "nexus_blobstore_file" "hosted" {
  name = "hosted"
  path = "hosted"

  # Alert-only, like default's. Sized for the handful of CI-built images
  # (runner image + compta) across their live tags; bump if hosted usage
  # grows. Lives on the services dataset, which has its own ZFS quota.
  soft_quota {
    type  = "spaceUsedQuota"
    limit = 20 * 1024 * 1024 * 1024 # 20 GiB
  }
}

# Lock in the current public-read posture of the lab Nexus: the rest of
# the homelab pulls from these proxies without basic auth, so anonymous
# access must stay on. Codifying this means a Nexus upgrade can't reset
# the default and silently break apt/podman across every host.
resource "nexus_security_anonymous" "this" {
  enabled    = true
  user_id    = "anonymous"
  realm_name = "NexusAuthorizingRealm"
}

# Pin the active *authentication* realms. NexusAuthorizingRealm is not on
# this list — it's the authorizer (used as nexus_security_anonymous.realm_name
# above) rather than something the user picks an auth method against.
resource "nexus_security_realms" "this" {
  active = [
    "NexusAuthenticatingRealm",
    "DockerToken",
  ]
}

locals {
  apt_proxies = {
    "ubuntu-archive"    = "https://archive.ubuntu.com/ubuntu/"
    "ubuntu-security"   = "https://security.ubuntu.com/ubuntu/"
    "ubuntu-ports"      = "https://ports.ubuntu.com/ubuntu-ports/"
    "azlux-debian"      = "https://packages.azlux.fr/debian/"
    "nodesource-node22" = "https://deb.nodesource.com/node_22.x/"
    "netdata"           = "https://repo.netdata.cloud/repos/stable/ubuntu/"
    "fluentbit"         = "https://packages.fluentbit.io/ubuntu/"
    "1password"         = "https://downloads.1password.com/linux/debian/amd64/"
    "docker-ce"         = "https://download.docker.com/linux/ubuntu/"
    "tailscale"         = "https://pkgs.tailscale.com/stable/ubuntu/"
    "mise"              = "https://mise.en.dev/deb/"
  }

  raw_proxies = {
    "github"                = "https://github.com/"
    "raw-githubusercontent" = "https://raw.githubusercontent.com/"
    "minio"                 = "https://dl.min.io/"
    "gitea-dl"              = "https://dl.gitea.com/"
    "gitea-com"             = "https://gitea.com/"
    "gitea-lab"             = "https://gitea.lab.fahm.fr/"
    "ubuntu-releases"       = "https://releases.ubuntu.com/"
    "ubuntu-cdimage"        = "https://cdimage.ubuntu.com/"
    "ubuntu-cloud-images"   = "https://cloud-images.ubuntu.com/"
  }

  # Raw proxies whose upstream serves content with Content-Type headers that
  # don't match the filename extensions; strict validation rejects those
  # responses. Add a key here when a proxy needs it; everyone else stays strict.
  raw_proxies_loose_content_type = toset(["ubuntu-cloud-images", "raw-githubusercontent"])

  # The datadrivers/nexus provider does not expose nexus_repository_cleanup_policy
  # as a managed resource, so the policy itself has to be created once via the
  # Nexus UI (Administration → Repository → Cleanup policies): name
  # "proxy-stale-365d", "all formats", criteria component usage > 365 days
  # (drops cached components not pulled in a year). Once it exists, every
  # proxy below references it via the cleanup block; the daily built-in
  # cleanup task drops the matching components.
  cleanup_policies = ["proxy-stale-365d"]

  docker_proxies = {
    "docker.io" = {
      remote_url = "https://registry-1.docker.io"
      index_type = "HUB"
      index_url  = null
    }
    "ghcr.io" = {
      remote_url = "https://ghcr.io"
      index_type = "REGISTRY"
      index_url  = null
    }
  }
}

resource "nexus_repository_apt_proxy" "this" {
  for_each = local.apt_proxies

  name         = each.key
  online       = true
  distribution = "*"
  flat         = false

  storage {
    blob_store_name                = "default"
    strict_content_type_validation = true
  }
  negative_cache {
    enabled = true
    ttl     = 60
  }
  http_client {
    blocked    = false
    auto_block = false
  }
  proxy {
    remote_url       = each.value
    content_max_age  = 525600
    metadata_max_age = 60
  }
  cleanup {
    policy_names = local.cleanup_policies
  }
}

resource "nexus_repository_pypi_proxy" "pypi" {
  name   = "pypi"
  online = true

  storage {
    blob_store_name                = "default"
    strict_content_type_validation = true
  }
  negative_cache {
    enabled = true
    ttl     = 60
  }
  http_client {
    blocked    = false
    auto_block = false
  }
  proxy {
    remote_url       = "https://pypi.org/"
    content_max_age  = 525600
    metadata_max_age = 60
  }
  cleanup {
    policy_names = local.cleanup_policies
  }
}

resource "nexus_repository_raw_proxy" "this" {
  for_each = local.raw_proxies

  name   = each.key
  online = true

  storage {
    blob_store_name                = "default"
    strict_content_type_validation = !contains(local.raw_proxies_loose_content_type, each.key)
  }
  negative_cache {
    enabled = true
    ttl     = 60
  }
  http_client {
    blocked    = false
    auto_block = false
  }
  proxy {
    remote_url       = each.value
    content_max_age  = 525600
    metadata_max_age = 60
  }
  cleanup {
    policy_names = local.cleanup_policies
  }
}

# Hosted (not proxy) docker repos for images we build off-host:
#   - homelab: the ci-image workflow pushes
#     nexus.lab.fahm.fr/homelab/ci:<sha> + :latest after
#     rebuilding the runner image.
#   - compta: the adrienkohlbecker/compta repo's build workflow
#     pushes nexus.lab.fahm.fr/compta/compta:<sha> +
#     :latest; the compta ansible role pulls from there.
# force_basic_auth=false (below) keeps the DockerToken realm active so
# anonymous bearer-token pulls work (unauthenticated `podman pull` from
# CI / lab hosts). Pushes still require basic auth because the only role
# carrying nx-repository-view-docker-<name>-{add,edit} is <name>-push,
# bound to the dedicated push user below -- the anonymous role has no
# such privileges, so an anonymous bearer token can pull but not push.
#
# write_policy ALLOW (not ALLOW_ONCE) because the workflows re-push
# :latest on every successful build. ALLOW_ONCE would reject the
# second push.
#
# These ride the snapshotted "hosted" blob store (nexus_blobstore_file.hosted),
# not the scratch-backed "default". OSS Nexus locks a repo's blob store after
# creation (the online "Change Repository Blob Store" task is Pro-only), so
# migrating an existing repo is a delete+recreate that starts it empty:
#   tofu apply \
#     -replace='nexus_repository_docker_hosted.this["homelab"]' \
#     -replace='nexus_repository_docker_hosted.this["compta"]'
# then re-push: dispatch the ci-image workflow (rebuilds homelab/ci) and the
# adrienkohlbecker/compta build (re-push the version the compta role pins).
# Orphaned blobs left in "default" are reclaimed by the blob store compact
# task. A plain `tofu apply` cannot repoint a live repo -- it needs -replace.
resource "nexus_repository_docker_hosted" "this" {
  for_each = toset(["homelab", "compta"])

  name   = each.key
  online = true

  # TODO: codify pathEnabled=true once the datadrivers/nexus provider
  # ships path_based_routing in a release (already on main, not yet in
  # v2.7.1). Currently toggled manually in the Nexus UI on both repos so
  # the docker client can push to nexus.lab.fahm.fr/<repo>/<image>:tag
  # without the /repository/ URL prefix going through an nginx rewrite.
  # The provider leaves the field alone (it isn't in v2.7.1's schema),
  # so apply is a no-op and the manual setting persists.
  docker {
    force_basic_auth = false
    v1_enabled       = false
  }
  storage {
    blob_store_name                = nexus_blobstore_file.hosted.name
    strict_content_type_validation = true
    write_policy                   = "ALLOW"
  }
}

# Explicit per-repo view privilege, rather than leaning on the
# nx-repository-view-docker-<repo>-* privilege Nexus auto-creates with each
# repo. The auto-created one is deleted -- and silently stripped from any role
# that references it -- when the repo is deleted (e.g. a blob-store -replace),
# leaving the push role with no grant until re-added by hand. A terraform-owned
# privilege is reconciled by `tofu apply`, and the push role below binds to a
# graph resource instead of a magic string the provider can't see. Full action
# set (browse/read/edit/add/delete) matches what the wildcard granted.
resource "nexus_privilege_repository_view" "docker_hosted" {
  for_each = nexus_repository_docker_hosted.this

  name        = "${each.key}-docker-all"
  description = "Full view of the ${each.key} docker repo (browse/read/edit/add/delete)"
  repository  = each.value.name
  format      = "docker"
  actions     = ["BROWSE", "READ", "EDIT", "ADD", "DELETE"]
}

# Least-privileged role + user pair per docker hosted repo. The role grants the
# explicit per-repo view privilege above and nothing else: no admin / config
# rights, no scope outside the named repo. One user per repo so a leaked
# credential is scoped to a single repo's images. for_each is keyed off the
# docker_hosted resource so adding a hosted repo above automatically provisions
# its privilege, push role + user.
resource "nexus_security_role" "push" {
  for_each = nexus_repository_docker_hosted.this

  roleid      = "${each.key}-push"
  name        = "${each.key}-push"
  description = "Push images to the ${each.key} docker hosted repo"
  privileges = [
    nexus_privilege_repository_view.docker_hosted[each.key].name,
  ]
  roles = []
}

# Single role granting content access to *every* repository, any format, so the
# operator's all-repos identity never needs a per-repo or per-format edit.
# nexus:repository-view:* is the Shiro form of nx-repository-view-*: it matches
# browse/read/edit/add/delete on every repo, but not admin/config (that lives
# under nexus:* and the application privileges). Deliberately broad -- it's the
# operator's own login, not a service account; the scoped *-push roles above
# still constrain the CI credentials.
resource "nexus_privilege_wildcard" "repository_view_all" {
  name        = "repository-view-all"
  description = "View (browse/read/edit/add/delete) on all repositories, all formats"
  pattern     = "nexus:repository-view:*"
}

resource "nexus_security_role" "push_all" {
  roleid      = "push-all"
  name        = "push-all"
  description = "Push to (and manage content of) every repository"
  privileges  = [nexus_privilege_wildcard.repository_view_all.name]
  roles       = []
}

# Generated once and pinned in encrypted state; rotate by tainting the
# random_password resource (`tofu apply -replace='random_password.
# nexus_push["<name>"]'`). 32 chars, alphanumeric only -- some HTTP
# basic auth shells (and the docker `--password-stdin` round-trip)
# misbehave on punctuation.
resource "random_password" "nexus_push" {
  for_each = nexus_repository_docker_hosted.this

  length  = 32
  special = false
}

resource "nexus_security_user" "push" {
  for_each = nexus_repository_docker_hosted.this

  userid    = "${each.key}-push"
  firstname = each.key
  lastname  = "push"
  email     = "${each.key}-push@noreply.invalid"
  password  = random_password.nexus_push[each.key].result
  roles     = [nexus_security_role.push[each.key].roleid]
  status    = "active"

  lifecycle {
    replace_triggered_by = [random_password.nexus_push[each.key]]
  }
}

# Operator's local/manual docker push identity (you `podman login` as this from
# your workstation and lab hosts), carrying the push-all role so it can push to
# every repository without a per-repo edit here. Adopted from the user
# created by hand in the UI, not provisioned here, so its password is left
# unmanaged: `password` is omitted, the Nexus API never returns it, and the
# provider can't see it -- a plain apply leaves the credential untouched and
# only reconciles role membership. The import block adopts the existing user in
# place on the next apply (no recreate); delete it once the import has run.
import {
  to = nexus_security_user.lab_local_user
  id = "lab_local_user"
}

resource "nexus_security_user" "lab_local_user" {
  userid    = "lab_local_user"
  firstname = "Lab"
  lastname  = "Local User"
  email     = "lab_local_user@noreply.invalid"
  status    = "active"
  roles = [
    nexus_security_role.push_all.roleid,
  ]
}

# Raw hosted repo for the ZFSBootMenu CI validation build (zbm-build.yml). The
# job builds the recovery image with a THROWAWAY host key and PUTs the tarball
# here for inspection -- private (on-lab), unlike a public GitHub artifact, and
# NOT a release (the real, stable-host-key tarball still ships to Gitea via
# `mise run zbm:upload`). Rides the snapshotted "hosted" blob store, same
# blob-store-lock caveat as the docker hosted repos above (changing it is a
# delete+recreate via -replace). write_policy=ALLOW so each run overwrites the
# stable per-(version,arch) path instead of accumulating; strict content-type
# off because the payload is a plain tarball.
resource "nexus_repository_raw_hosted" "zbm" {
  name   = "zbm"
  online = true

  storage {
    blob_store_name                = nexus_blobstore_file.hosted.name
    strict_content_type_validation = false
    write_policy                   = "ALLOW"
  }
}

# Least-privileged push identity for the zbm raw repo, mirroring the per-docker
# -repo privilege/role/user pattern above: one scoped credential so a leak
# reaches only this repo. Consumed by zbm-build.yml as NEXUS_ZBM_USERNAME /
# NEXUS_ZBM_PASSWORD (provisioned into the homelab repo in github.tf).
resource "nexus_privilege_repository_view" "zbm_raw" {
  name        = "zbm-raw-push"
  description = "Push artifacts to the zbm raw hosted repo (browse/read/add)"
  repository  = nexus_repository_raw_hosted.zbm.name
  format      = "raw"
  actions     = ["BROWSE", "READ", "ADD"]
}

resource "nexus_security_role" "zbm_push" {
  roleid      = "zbm-push"
  name        = "zbm-push"
  description = "Push artifacts to the zbm raw hosted repo"
  privileges  = [nexus_privilege_repository_view.zbm_raw.name]
  roles       = []
}

resource "random_password" "nexus_zbm_push" {
  length  = 32
  special = false
}

resource "nexus_security_user" "zbm_push" {
  userid    = "zbm-push"
  firstname = "zbm"
  lastname  = "push"
  email     = "zbm-push@noreply.invalid"
  password  = random_password.nexus_zbm_push.result
  roles     = [nexus_security_role.zbm_push.roleid]
  status    = "active"

  lifecycle {
    replace_triggered_by = [random_password.nexus_zbm_push]
  }
}

resource "nexus_repository_docker_proxy" "this" {
  for_each = local.docker_proxies

  name   = each.key
  online = true

  docker {
    force_basic_auth = false
    v1_enabled       = false
  }
  docker_proxy {
    index_type = each.value.index_type
    index_url  = each.value.index_url
  }
  storage {
    blob_store_name                = "default"
    strict_content_type_validation = true
  }
  negative_cache {
    enabled = true
    ttl     = 60
  }
  http_client {
    blocked    = false
    auto_block = false
  }
  proxy {
    remote_url       = each.value.remote_url
    content_max_age  = 525600
    metadata_max_age = 60
  }
  cleanup {
    policy_names = local.cleanup_policies
  }
}
