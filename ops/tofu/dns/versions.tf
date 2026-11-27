terraform {
  backend "s3" {}

  required_version = "~> 1.13.0"

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "5.29.0"
    }
  }
}

# The token comes from CLOUDFLARE_API_TOKEN.
provider "cloudflare" {}
