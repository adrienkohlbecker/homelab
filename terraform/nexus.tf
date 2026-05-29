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
    auto_block = true
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
    auto_block = true
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
    auto_block = true
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
    blob_store_name                = "default"
    strict_content_type_validation = true
    write_policy                   = "ALLOW"
  }
}

# Least-privileged role + user pair per docker hosted repo. The wildcard
# view privilege covers browse / read / edit / add / delete on the named
# repo (Nexus auto-creates these per repo); no admin / config rights, and
# no scope outside the named repo. One user per repo so a leaked
# credential is scoped to a single repo's images. for_each is keyed off
# the docker_hosted resource so adding a hosted repo above automatically
# provisions its push role + user.
resource "nexus_security_role" "push" {
  for_each = nexus_repository_docker_hosted.this

  roleid      = "${each.key}-push"
  name        = "${each.key}-push"
  description = "Push images to the ${each.key} docker hosted repo"
  privileges = [
    "nx-repository-view-docker-${each.key}-*",
  ]
  roles = []
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
    auto_block = true
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
