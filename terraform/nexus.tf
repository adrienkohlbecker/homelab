provider "nexus" {
  url = "https://nexus.lab.fahm.fr"
  # NEXUS_USERNAME / NEXUS_PASSWORD come from terraform/.env via `op run`.
}

resource "nexus_blobstore_file" "default" {
  name = "default"
  path = "default"

  # Banner-only warning when usage crosses this threshold; doesn't block
  # writes. Sized for the /mnt/scratch zvol the store now lives on —
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

# Hosted (not proxy) docker repo for images we build in CI. Today the
# only consumer is the lab-runtime-build workflow, which pushes
# nexus.lab.fahm.fr/repository/homelab/lab-runtime:<sha> + :latest after
# rebuilding the runner image. Anonymous pulls are on (so unauthenticated
# `podman pull` from CI / lab hosts works); push requires basic auth
# against a Nexus user with nx-repository-edit-docker-homelab-add /
# -edit / -delete privileges (created once via the Nexus UI; the
# datadrivers provider does not yet expose user/role/privilege
# resources). Force-basic-auth ON keeps anonymous Bearer-token paths
# from accidentally accepting pushes.
#
# write_policy ALLOW (not ALLOW_ONCE) because the lab-runtime-build
# workflow re-pushes :latest on every successful build. ALLOW_ONCE
# would reject the second push.
resource "nexus_repository_docker_hosted" "this" {
  for_each = toset(["homelab"])

  name   = each.key
  online = true

  docker {
    force_basic_auth = true
    v1_enabled       = false
  }
  storage {
    blob_store_name                = "default"
    strict_content_type_validation = true
    write_policy                   = "ALLOW"
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
