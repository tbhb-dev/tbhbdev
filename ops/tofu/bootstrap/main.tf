# The R2 bucket that holds the state of the other roots, the token that
# reaches it, and the 1Password item that carries the S3 credentials.
resource "cloudflare_r2_bucket" "state" {
  account_id = var.account_id
  name       = var.state_bucket_name

  lifecycle {
    prevent_destroy = true
  }
}

data "cloudflare_account_api_token_permission_groups_list" "bucket_write" {
  account_id = var.account_id
  name       = "Workers R2 Storage Bucket Item Write"
}

data "cloudflare_account_api_token_permission_groups_list" "bucket_read" {
  account_id = var.account_id
  name       = "Workers R2 Storage Bucket Item Read"
}

resource "cloudflare_account_token" "state" {
  account_id = var.account_id
  name       = var.state_bucket_name

  policies = [{
    effect = "allow"
    permission_groups = [
      { id = one(data.cloudflare_account_api_token_permission_groups_list.bucket_write.result).id },
      { id = one(data.cloudflare_account_api_token_permission_groups_list.bucket_read.result).id },
    ]
    resources = jsonencode({
      "com.cloudflare.edge.r2.bucket.${var.account_id}_default_${cloudflare_r2_bucket.state.name}" = "*"
    })
  }]
}

data "onepassword_vault" "ops" {
  name = var.password_vault_name
}

# A secure note takes sections and fields without requiring a username or
# password, which an S3 key pair doesn't fit.
resource "onepassword_item" "state_bucket" {
  vault    = data.onepassword_vault.ops.uuid
  title    = var.state_item_title
  category = "secure_note"

  section_map = {
    s3 = {
      field_map = {
        access_key_id = {
          type  = "STRING"
          value = cloudflare_account_token.state.id
        }
        # R2 takes the SHA-256 of the token value as the S3 secret.
        secret_access_key = {
          type  = "CONCEALED"
          value = sha256(cloudflare_account_token.state.value)
        }
        endpoint = {
          type  = "URL"
          value = "https://${var.account_id}.r2.cloudflarestorage.com"
        }
        bucket = {
          type  = "STRING"
          value = cloudflare_r2_bucket.state.name
        }
      }
    }
  }
}

variable "state_bucket_name" {
  description = "Bucket name for remote OpenTofu state."
  type        = string
}

variable "password_vault_name" {
  description = "Name of the deployment credential vault."
  type        = string
}

variable "state_item_title" {
  description = "Item title for the state credentials."
  type        = string
}
