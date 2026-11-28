# The Bazel remote cache: a bucket and lifecycle per environment, the bearer
# tokens that the cache Workers check, and the 1Password items that carry them.
resource "cloudflare_r2_bucket" "cache" {
  for_each = local.environments

  account_id = var.account_id
  name       = "bazel-r2-cache-${each.key}"

  lifecycle {
    prevent_destroy = true
  }
}

resource "cloudflare_r2_bucket_lifecycle" "cache" {
  for_each = local.environments

  account_id  = var.account_id
  bucket_name = cloudflare_r2_bucket.cache[each.key].name

  # The API returns rules sorted by id, so they go in that order to keep the
  # plan clean.
  rules = [
    for id in sort(keys(local.lifecycle_rules[each.key])) :
    local.lifecycle_rules[each.key][id]
  ]
}

# URL-safe because special characters are off.
resource "random_password" "token" {
  for_each = local.tokens

  length  = 48
  special = false
}

data "onepassword_vault" "cache" {
  name = "bazel-r2-cache"
}

# CLOUDFLARE_API_TOKEN joins once the environment's Worker exists and has a
# deploy token (workers.tf).
resource "onepassword_item" "env" {
  for_each = local.environments

  vault    = data.onepassword_vault.cache.uuid
  title    = "bazel-r2-cache ${each.key}"
  category = "secure_note"

  section_map = {
    cache = {
      field_map = merge({
        CLOUDFLARE_ACCOUNT_ID = {
          type  = "STRING"
          value = var.account_id
        }
        READ_TOKENS = {
          type = "CONCEALED"
          value = join(",", [
            for consumer in local.config.read_consumers :
            random_password.token["${each.key}/${consumer}"].result
          ])
        }
        HOST_TOKENS = {
          type = "CONCEALED"
          value = join(",", [
            for host in local.config.host_consumers :
            random_password.token["${each.key}/host-${host}"].result
          ])
        }
        MAIN_TOKEN = {
          type  = "CONCEALED"
          value = random_password.token["${each.key}/main"].result
        }
        PR_TOKEN = {
          type  = "CONCEALED"
          value = random_password.token["${each.key}/pr"].result
        }
        E2E_TOKEN = {
          type  = "CONCEALED"
          value = random_password.token["${each.key}/e2e"].result
        }
        }, {
        for env in [each.key] : "CLOUDFLARE_API_TOKEN" => {
          type  = "CONCEALED"
          value = cloudflare_account_token.deploy[env].value
        } if contains(var.deployed_workers, env)
      })
    }
  }
}

resource "onepassword_item" "read" {
  for_each = local.read_tokens

  vault    = data.onepassword_vault.cache.uuid
  title    = "bazel-r2-cache ${each.value.env} read ${each.value.consumer}"
  category = "secure_note"

  section_map = {
    cache = {
      field_map = {
        token = {
          type  = "CONCEALED"
          value = random_password.token[each.key].result
        }
      }
    }
  }
}

resource "onepassword_item" "host" {
  for_each = local.host_tokens

  vault    = data.onepassword_vault.cache.uuid
  title    = "bazel-r2-cache ${each.value.env} host ${each.value.host}"
  category = "secure_note"

  section_map = {
    cache = {
      field_map = {
        token = {
          type  = "CONCEALED"
          value = random_password.token[each.key].result
        }
      }
    }
  }
}
