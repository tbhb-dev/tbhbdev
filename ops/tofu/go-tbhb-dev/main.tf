# The 1Password items that carry each environment's deploy credentials.
data "onepassword_vault" "go" {
  name = var.password_vault_name
}

resource "onepassword_item" "env" {
  for_each = local.environments

  vault    = data.onepassword_vault.go.uuid
  title    = "go-tbhb-dev ${each.key}"
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
      }
    }
  }
}

variable "password_vault_name" {
  description = "Name of the deployment credential vault."
  type        = string
}
