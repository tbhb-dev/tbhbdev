# Edge rules for the host names the Bazel cache, Go vanity import and awrel
# Workers answer on, and the events tunnel. The github-metrics host is listed
# only in the API host rule. Requests a Worker would refuse anyway stop here,
# so most drive-bys never run a Worker.
locals {
  cache_hosts = var.cache_hosts
  go_hosts    = var.go_hosts
  awrel_hosts = var.awrel_hosts

  # Served by a tunnel and CNAME outside this repository.
  github_metrics_hosts = var.github_metrics_hosts

  # Served by a tunnel and CNAME created by admin-vm outside this repository,
  # like github-metrics.
  events_hosts = var.events_hosts

  cache_host_match  = "http.host in {${join(" ", formatlist("\"%s\"", local.cache_hosts))}}"
  go_host_match     = "http.host in {${join(" ", formatlist("\"%s\"", local.go_hosts))}}"
  awrel_host_match  = "http.host in {${join(" ", formatlist("\"%s\"", local.awrel_hosts))}}"
  events_host_match = "http.host in {${join(" ", formatlist("\"%s\"", local.events_hosts))}}"
  api_host_match    = "http.host in {${join(" ", formatlist("\"%s\"", concat(local.cache_hosts, local.go_hosts, local.awrel_hosts, local.github_metrics_hosts, local.events_hosts)))}}"

  awrel_webhook = "http.request.method eq \"POST\" and starts_with(http.request.uri.path, \"/webhooks/\")"
}

resource "cloudflare_ruleset" "firewall_custom" {
  zone_id = cloudflare_zone.main.id
  name    = "default"
  kind    = "zone"
  phase   = "http_request_firewall_custom"

  rules = [
    {
      # The cache Worker answers 404 to any path but /ac/ or /cas/ with a
      # 64-character digest, before it looks at the token or the method.
      # Matching only that keeps every other answer, such as the 401 the
      # e2e check expects without a token, coming from the Worker.
      description = "Bazel cache: refuse paths the Worker doesn't serve"
      expression = join(" ", [
        "${local.cache_host_match} and not (",
        "(starts_with(http.request.uri.path, \"/ac/\") and len(http.request.uri.path) eq 68)",
        "or (starts_with(http.request.uri.path, \"/cas/\") and len(http.request.uri.path) eq 69)",
        ")",
      ])
      action = "block"
      action_parameters = {
        response = {
          status_code  = 404
          content_type = "text/plain"
          content      = "not found"
        }
      }
    },
    {
      description = "Go vanity imports: GET and HEAD only"
      expression  = "${local.go_host_match} and not http.request.method in {\"GET\" \"HEAD\"}"
      action      = "block"
    },
    {
      # awrel takes webhooks, which providers can't authenticate at the edge,
      # and the MCP server's WebSocket on /connect. The Worker checks the
      # webhook signature and the /connect bearer token.
      description = "awrel: webhooks and the /connect WebSocket only"
      expression = join(" ", [
        "${local.awrel_host_match} and not (",
        "(${local.awrel_webhook})",
        "or (http.request.method eq \"GET\" and http.request.uri.path eq \"/connect\"",
        "and any(http.request.headers[\"upgrade\"][*] in {\"websocket\" \"WebSocket\"}))",
        ")",
      ])
      action = "block"
      action_parameters = {
        response = {
          status_code  = 404
          content_type = "text/plain"
          content      = "not found"
        }
      }
    },
    {
      description = "events: POST /webhooks/github only"
      expression  = "${local.events_host_match} and not (http.request.method eq \"POST\" and http.request.uri.path eq \"/webhooks/github\")"
      action      = "block"
    },
  ]
}

resource "cloudflare_ruleset" "ratelimit" {
  zone_id = cloudflare_zone.main.id
  name    = "default"
  kind    = "zone"
  phase   = "http_ratelimit"

  rules = [
    {
      # The Go module proxy and go get fetch a handful of pages per module,
      # and a provider sends one webhook per event. Pro allows two
      # rate-limiting rules, so the two share this one.
      description = "Go vanity imports and awrel webhooks: per-IP rate limit"
      expression  = "${local.go_host_match} or (${local.awrel_host_match} and ${local.awrel_webhook})"
      action      = "block"
      ratelimit = {
        characteristics     = ["ip.src", "cf.colo.id"]
        period              = 60
        requests_per_period = 300
        mitigation_timeout  = 60
      }
    },
    {
      # Only well-formed cache paths get this far, and a warm build sends
      # thousands of them, so this only stops a flood.
      description = "Bazel cache: per-IP flood backstop"
      expression  = local.cache_host_match
      action      = "block"
      ratelimit = {
        characteristics     = ["ip.src", "cf.colo.id"]
        period              = 60
        requests_per_period = 30000
        mitigation_timeout  = 60
      }
    },
  ]
}

# Bazel and the Go toolchain can't solve a challenge, so the API host names
# never issue one.
resource "cloudflare_ruleset" "config_settings" {
  zone_id = cloudflare_zone.main.id
  name    = "default"
  kind    = "zone"
  phase   = "http_config_settings"

  rules = [
    {
      description = "API host names: no challenges"
      expression  = local.api_host_match
      action      = "set_config"
      action_parameters = {
        security_level = "essentially_off"
        bic            = false
      }
    },
  ]
}

variable "cache_hosts" {
  description = "Hostnames for the cache firewall policy."
  type        = list(string)
}

variable "go_hosts" {
  description = "Hostnames for the go firewall policy."
  type        = list(string)
}

variable "awrel_hosts" {
  description = "Hostnames for the awrel firewall policy."
  type        = list(string)
}

variable "github_metrics_hosts" {
  description = "Hostnames for the github metrics firewall policy."
  type        = list(string)
}

variable "events_hosts" {
  description = "Hostnames for the events firewall policy."
  type        = list(string)
}
