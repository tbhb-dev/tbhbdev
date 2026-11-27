variable "account_id" {
  description = "Cloudflare account ID."
  type        = string
}

variable "state_passphrase" {
  description = "Passphrase for the state and plan encryption key."
  type        = string
  sensitive   = true
}
