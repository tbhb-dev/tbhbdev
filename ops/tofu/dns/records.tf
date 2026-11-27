resource "cloudflare_dns_record" "main" {
  for_each = local.zone.records

  zone_id = cloudflare_zone.main.id
  name    = each.value.name
  type    = each.value.type
  content = each.value.content
  ttl     = each.value.ttl
  proxied = false
}
