terraform {
  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5"
    }
  }

  required_version = ">= 1.11.4"
}

data "cloudflare_account" "main" {
  filter = {
    name = "Home"
  }
}

resource "cloudflare_zone" "fahm_dev" {
  account = {
    id = "${data.cloudflare_account.main.account_id}"
  }
  name = "fahm.dev"
  type = "full"
}

resource "cloudflare_zone" "fahm_fr" {
  account = {
    id = "${data.cloudflare_account.main.account_id}"
  }
  name = "fahm.fr"
  type = "full"
}

resource "cloudflare_dns_record" "star_box_fahm_dev" {
  zone_id = "${cloudflare_zone.fahm_dev.id}"
  content = "box.fahm.dev"
  name = "*.box.fahm.dev"
  proxied = false
  ttl = 1
  type = "CNAME"
}

resource "cloudflare_dns_record" "box_fahm_dev" {
  zone_id = "${cloudflare_zone.fahm_dev.id}"
  content = "10.234.0.5"
  name = "box.fahm.dev"
  proxied = false
  ttl = 1
  type = "A"
}
