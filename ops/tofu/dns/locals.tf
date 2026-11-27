locals {
  zone = yamldecode(file(var.zone_config_path))

  email = local.zone.email_routing
  rules = { for rule in local.email.rules : rule.key => rule }
}

variable "zone_config_path" {
  description = "Path to the companion DNS zone and mail settings."
  type        = string
}
