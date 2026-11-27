# The 1Password items that carry each environment's deploy credentials and
# Worker secrets.
data "onepassword_vault" "awrel" {
  name = var.password_vault_name
}

# The GitHub webhook secret and the bearer token the MCP server presents on
# /connect. URL-safe because special characters are off.
resource "random_password" "github_webhook_secret" {
  for_each = local.environments

  length  = 48
  special = false
}

resource "random_password" "connect_token" {
  for_each = local.environments

  length  = 48
  special = false
}

resource "onepassword_item" "env" {
  for_each = local.environments

  vault    = data.onepassword_vault.awrel.uuid
  title    = "awrel ${each.key}"
  category = "secure_note"

  section_map = {
    deploy = {
      field_map = {
        CLOUDFLARE_API_TOKEN = {
          type  = "CONCEALED"
          value = cloudflare_account_token.deploy[each.key].value
        }
        CLOUDFLARE_ACCOUNT_ID = {
          type  = "STRING"
          value = var.account_id
        }
        GITHUB_WEBHOOK_SECRET = {
          type  = "CONCEALED"
          value = random_password.github_webhook_secret[each.key].result
        }
        CONNECT_TOKEN = {
          type  = "CONCEALED"
          value = random_password.connect_token[each.key].result
        }
      }
    }
  }
}

variable "password_vault_name" {
  description = "Name of the deployment credential vault."
  type        = string
}
