# The Workers awrel-<env> are deployed by wrangler, not managed here.
# Each one gets a deploy token scoped to its script and a custom domain.
data "cloudflare_account_api_token_permission_groups_list" "worker_editor" {
  account_id = var.account_id
  name       = "Individual Workers Editor"
}

data "cloudflare_workers_scripts" "awrel" {
  account_id = var.account_id
}

locals {
  worker_tags = {
    for env in keys(local.environments) : env => one([
      for script in data.cloudflare_workers_scripts.awrel.result :
      script.tag if script.id == "awrel-${env}"
    ])
  }
}

resource "cloudflare_account_token" "deploy" {
  for_each = local.environments

  account_id = var.account_id
  name       = "awrel-${each.key}-deploy"

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

resource "cloudflare_workers_custom_domain" "awrel" {
  for_each = local.environments

  account_id = var.account_id
  zone_id    = var.zone_id
  hostname   = each.value
  service    = "awrel-${each.key}"
}

variable "zone_id" {
  description = "Cloudflare zone for the worker domains."
  type        = string
}
