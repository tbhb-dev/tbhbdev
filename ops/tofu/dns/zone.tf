resource "cloudflare_zone" "main" {
  account = { id = var.account_id }
  name    = local.zone.name
  type    = "full"

  lifecycle {
    prevent_destroy = true
  }
}

resource "cloudflare_zone_setting" "main" {
  for_each = local.zone.settings

  zone_id    = cloudflare_zone.main.id
  setting_id = each.key
  value      = each.value
}

# DNSSEC stays disabled until a DS record is published at the registrar.
resource "cloudflare_zone_dnssec" "main" {
  zone_id = cloudflare_zone.main.id
  status  = "disabled"
}
