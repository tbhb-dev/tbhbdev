# The Workers go-tbhb-dev-<env> are deployed by wrangler, not managed here.
# Each one gets a deploy token scoped to its script and a custom domain.
data "cloudflare_account_api_token_permission_groups_list" "worker_editor" {
  account_id = var.account_id
  name       = "Individual Workers Editor"
}

data "cloudflare_workers_scripts" "go" {
  account_id = var.account_id
}

locals {
  worker_tags = {
    for env in keys(local.environments) : env => one([
      for script in data.cloudflare_workers_scripts.go.result :
      script.tag if script.id == "go-tbhb-dev-${env}"
    ])
  }
}

resource "cloudflare_account_token" "deploy" {
  for_each = local.environments

  account_id = var.account_id
  name       = "go-tbhb-dev-${each.key}-deploy"

  policies = [{
    effect = "allow"
    permission_groups = [
      { id = one(data.cloudflare_account_api_token_permission_groups_list.worker_editor.result).id },
    ]
    resources = jsonencode({
      "com.cloudflare.api.account.${var.account_id}" = {
        "com.cloudflare.edge.worker.script.${local.worker_tags[each.key]}" = "*"
      }
    })
  }]
}

resource "cloudflare_workers_custom_domain" "go" {
  for_each = local.environments

  account_id = var.account_id
  zone_id    = var.zone_id
  hostname   = each.value
  service    = "go-tbhb-dev-${each.key}"
}

variable "zone_id" {
  description = "Cloudflare zone for the worker domains."
  type        = string
}
