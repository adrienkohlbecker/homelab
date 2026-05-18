resource "cloudflare_zone" "fahm_fr" {
  account = {
    id = data.cloudflare_account.main.account_id
  }
  name = "fahm.fr"
  type = "full"
}

resource "cloudflare_zone" "mhaf_fr" {
  account = {
    id = data.cloudflare_account.main.account_id
  }
  name = "mhaf.fr"
  type = "full"
}

resource "cloudflare_zone" "adrienkohlbecker_com" {
  account = {
    id = data.cloudflare_account.main.account_id
  }
  name = "adrienkohlbecker.com"
  type = "full"
}
