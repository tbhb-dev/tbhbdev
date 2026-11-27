locals {
  environments = var.worker_domains
}

variable "worker_domains" {
  description = "Worker hostname by environment."
  type        = map(string)
}
