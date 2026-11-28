locals {
  config = yamldecode(file(var.environments_path))

  environments = local.config.environments

  # One token per environment and role, keyed "<env>/<role>". A host token
  # is keyed "<env>/host-<name>", since its host is also a read consumer.
  roles = concat(local.config.read_consumers, local.config.write_roles)
  host_tokens = {
    for pair in setproduct(keys(local.environments), local.config.host_consumers) :
    "${pair[0]}/host-${pair[1]}" => { env = pair[0], host = pair[1] }
  }
  tokens = merge(
    {
      for pair in setproduct(keys(local.environments), local.roles) :
      "${pair[0]}/${pair[1]}" => { env = pair[0], role = pair[1] }
    },
    {
      for key, token in local.host_tokens :
      key => { env = token.env, role = "host-${token.host}" }
    },
  )
  # Lifecycle rules per environment, keyed by id: the age-based deletes from
  # environments.yaml plus a bucket-wide abort of stale multipart uploads.
  lifecycle_rules = {
    for env, cfg in local.environments : env => merge(
      {
        for rule in cfg.lifecycle : rule.id => {
          id                                 = rule.id
          enabled                            = true
          conditions                         = { prefix = rule.prefix }
          delete_objects_transition          = { condition = { type = "Age", max_age = rule.days * 86400 } }
          abort_multipart_uploads_transition = null
        }
      },
      {
        "abort-multipart-1d" = {
          id                                 = "abort-multipart-1d"
          enabled                            = true
          conditions                         = { prefix = "" }
          delete_objects_transition          = null
          abort_multipart_uploads_transition = { condition = { type = "Age", max_age = 86400 } }
        }
      },
    )
  }

  read_tokens = {
    for pair in setproduct(keys(local.environments), local.config.read_consumers) :
    "${pair[0]}/${pair[1]}" => { env = pair[0], consumer = pair[1] }
  }
}

variable "environments_path" {
  description = "Path to the companion environments, consumers and lifecycle settings."
  type        = string
}
