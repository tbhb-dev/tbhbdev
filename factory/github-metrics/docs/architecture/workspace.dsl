workspace "github-metrics" "Turns GitHub pull request activity into OpenTelemetry traces, metrics, and logs. Today only the receiver, request budget, and GitHub client exist. The rest is design." {

    model {
        engineer = person "Engineer" "Reads pull request timelines, timing metrics, and service logs in the observability backends."

        github = softwareSystem "GitHub" "Sends pull request webhooks and serves the REST API." "External"

        githubMetrics = softwareSystem "github-metrics" "Receives webhooks today. Design: reconciles pull request state from REST and exports telemetry." {
            tunnel = container "Tunnel connector" "Routes only POST /webhook from the public hostname to the service. Makes outbound connections only." "cloudflared"
            service = container "Service" "One static binary. Today it has the webhook receiver. Design: a worker and a reconciler." "Go" {
                receiver = component "Webhook receiver" "Verifies the signature while streaming the body, checks the delivery and event headers, then answers 202 or 400." "net/http"
                worker = component "Worker" "Planned. Turns the current state of a pull request into spans and metric observations." "Go"
                reconciler = component "Reconciler" "Planned. Polls REST every 15 minutes, sweeps open pull requests nightly, and backfills new installations." "Go"
                limiter = component "Request budget" "One shared token bucket with four priorities." "internal/budget"
                client = component "GitHub client" "Conditional requests, paging, token renewal, and secondary limit backoff." "internal/ghclient"
            }
            store = container "State store" "Planned. Delivery queue, dedupe table, exported span IDs, metric milestones, watermarks, and entity tags." "bbolt file on a mounted volume" "Database"
        }

        collector = softwareSystem "Host collector" "Accepts OTLP over HTTP and fans out by signal." "External"
        tempo = softwareSystem "Tempo" "Stores traces." "External"
        mimir = softwareSystem "Mimir" "Stores metrics." "External"
        loki = softwareSystem "Loki" "Stores logs." "External"

        github -> tunnel "Delivers webhooks" "HTTPS"
        tunnel -> receiver "Forwards POST /webhook" "HTTP"
        receiver -> store "Planned: queues deliveries"
        worker -> store "Planned: reads queue, records spans"
        reconciler -> store "Planned: keeps watermarks"
        reconciler -> worker "Planned: schedules PRs"
        worker -> client "Planned: fetches PR state"
        reconciler -> client "Planned: lists PRs"
        client -> limiter "Waits for a budget slot"
        client -> github "Planned: reads PRs" "HTTPS, REST"
        worker -> collector "Planned: exports telemetry" "OTLP over HTTP"
        collector -> tempo "Traces"
        collector -> mimir "Metrics"
        collector -> loki "Logs"
        engineer -> tempo "Reads traces"
        engineer -> mimir "Reads metrics"
        engineer -> loki "Reads logs"
    }

    views {
        systemContext githubMetrics "SystemContext" {
            include *
            autolayout lr
        }

        container githubMetrics "Containers" {
            include *
            autolayout lr
        }

        component service "Components" {
            include *
            autolayout tb 200 120
        }

        styles {
            element "Person" {
                shape Person
            }
            element "External" {
                background #999999
                color #ffffff
            }
            element "Database" {
                shape Cylinder
            }
        }
    }
}
