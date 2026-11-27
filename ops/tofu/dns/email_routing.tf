resource "cloudflare_email_routing_settings" "main" {
  zone_id            = cloudflare_zone.main.id
  support_subaddress = local.email.support_subaddress
}

# Owns the MX, DKIM TXT and SPF TXT records that Email Routing needs.
resource "cloudflare_email_routing_dns" "main" {
  zone_id = cloudflare_zone.main.id
}

resource "cloudflare_email_routing_address" "main" {
  for_each = local.email.addresses

  account_id = var.account_id
  email      = each.value.email
}

resource "cloudflare_email_routing_rule" "main" {
  for_each = local.rules

  zone_id  = cloudflare_zone.main.id
  name     = each.value.name
  enabled  = each.value.enabled
  priority = each.value.priority

  matchers = [{
    type  = "literal"
    field = "to"
    value = each.value.address
  }]

  actions = [{
    type  = "forward"
    value = [each.value.forward_to]
  }]
}

resource "cloudflare_email_routing_catch_all" "main" {
  zone_id = cloudflare_zone.main.id
  enabled = false
  name    = ""
  matchers = [{
    type = "all"
  }]
  actions = [{
    type = "drop"
  }]
}
