# The environments whose Worker exists. Each one gets a deploy token scoped
# to its Worker and a custom domain. A Worker has to exist first, because the
# token's scope names its script tag; the first deploy of an environment is a
# manual bootstrap. Workers are named bazel-r2-cache-<env>.
variable "deployed_workers" {
  description = "Environments whose Worker exists."
  type        = list(string)
  default     = ["stg", "prd"]
}

variable "worker_domains" {
  description = "Worker hostname by environment."
  type        = map(string)
}

data "cloudflare_account_api_token_permission_groups_list" "worker_editor" {
  count = length(var.deployed_workers) > 0 ? 1 : 0

  account_id = var.account_id
  name       = "Individual Workers Editor"
}

data "cloudflare_workers_scripts" "cache" {
  count = length(var.deployed_workers) > 0 ? 1 : 0

  account_id = var.account_id
}

locals {
  worker_tags = {
    for env in var.deployed_workers : env => one([
      for script in data.cloudflare_workers_scripts.cache[0].result :
      script.tag if script.id == "bazel-r2-cache-${env}"
    ])
  }
}

resource "cloudflare_account_token" "deploy" {
  for_each = toset(var.deployed_workers)

  account_id = var.account_id
  name       = "bazel-r2-cache-${each.key}-deploy"

  policies = [{
    effect = "allow"
    permission_groups = [
      { id = one(data.cloudflare_account_api_token_permission_groups_list.worker_editor[0].result).id },
    ]
    resources = jsonencode({
      "com.cloudflare.api.account.${var.account_id}" = {
        "com.cloudflare.edge.worker.script.${local.worker_tags[each.key]}" = "*"
      }
    })
  }]
}

resource "cloudflare_workers_custom_domain" "cache" {
  for_each = toset(var.deployed_workers)

  account_id = var.account_id
  zone_id    = var.zone_id
  hostname   = var.worker_domains[each.key]
  service    = "bazel-r2-cache-${each.key}"
}

variable "zone_id" {
  description = "Cloudflare zone for the worker domains."
  type        = string
}
