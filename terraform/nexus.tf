provider "nexus" {
  url = "https://nexus.lab.fahm.fr"
  # NEXUS_USERNAME / NEXUS_PASSWORD come from terraform/.env via `op run`.
}

locals {
  apt_proxies = {
    "ubuntu-archive"    = "https://archive.ubuntu.com/ubuntu/"
    "ubuntu-security"   = "https://security.ubuntu.com/ubuntu/"
    "ubuntu-ports"      = "https://ports.ubuntu.com/ubuntu-ports/"
    "azlux-debian"      = "https://packages.azlux.fr/debian/"
    "nodesource-node22" = "https://deb.nodesource.com/node_22.x/"
    "vector"            = "https://apt.vector.dev"
    "netdata"           = "https://repo.netdata.cloud/repos/stable/ubuntu/"
    "1password"         = "https://downloads.1password.com/linux/debian/amd64/"
  }

  raw_proxies = {
    "github"            = "https://github.com/"
    "minio"             = "https://dl.min.io/"
    "gitea-dl"          = "https://dl.gitea.com/"
    "gitea-com"         = "https://gitea.com/"
    "gitea-lab"         = "https://gitea.lab.fahm.fr/"
    "keyserver-ubuntu"  = "https://keyserver.ubuntu.com/"
  }

  docker_proxies = {
    "docker.io" = {
      remote_url = "https://registry-1.docker.io"
      index_type = "HUB"
      index_url  = null
    }
    "ghcr.io" = {
      remote_url = "https://ghcr.io"
      index_type = "REGISTRY"
      index_url  = "https://ghcr.io"
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
}

resource "nexus_repository_raw_proxy" "this" {
  for_each = local.raw_proxies

  name   = each.key
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
    remote_url       = each.value
    content_max_age  = 525600
    metadata_max_age = 60
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
}
