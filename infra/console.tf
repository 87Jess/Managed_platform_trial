# Hosted deployment console: Cloud Run behind Identity-Aware Proxy (off by default).
# Users sign in with their Google identity; the console deploys with its own service account.

locals {
  console_name         = "agent-platform-console"
  console_image        = var.console_image != "" ? var.console_image : var.policy_image
  console_runtime      = google_service_account.agents[var.console_runtime_agent].email
  console_iap_audience = "/projects/${data.google_project.this.number}/locations/${var.region}/services/${local.console_name}"
  console_user_emails = [
    for member in var.console_users : lower(split(":", member)[1])
    if startswith(member, "user:")
  ]
}

resource "google_service_account" "console" {
  account_id   = "platform-console"
  display_name = "Managed agents: hosted deployment console"
}

# Create Agent Engine resources and sandboxes, and run the read-only preflight checks.
resource "google_project_iam_member" "console" {
  for_each = toset([
    "roles/aiplatform.user",
    "roles/serviceusage.serviceUsageViewer",
    "roles/compute.networkViewer",
    "roles/logging.viewer", # container logs of failed agent deployments
  ])
  project = var.project_id
  role    = each.value
  member  = google_service_account.console.member
}

# Deploying an agent attaches its runtime service account, which needs actAs on that account only.
resource "google_service_account_iam_member" "console_act_as" {
  for_each           = google_service_account.agents
  service_account_id = each.value.name
  role               = "roles/iam.serviceAccountUser"
  member             = google_service_account.console.member
}

resource "google_project_service_identity" "iap" {
  provider   = google-beta
  service    = "iap.googleapis.com"
  depends_on = [google_project_service.apis]
}

resource "google_cloud_run_v2_service" "console" {
  count               = var.deploy_console ? 1 : 0
  name                = local.console_name
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  iap_enabled         = true
  deletion_protection = true

  template {
    service_account = google_service_account.console.email
    timeout         = "300s"
    # Plans and staged uploads are held in memory: keep a single instance.
    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }

    containers {
      image   = local.console_image
      command = ["/app/.venv/bin/python", "-m", "managed_agents.console"]
      ports {
        container_port = 8080
      }
      resources {
        limits = { cpu = "1", memory = "1Gi" }
      }
      env {
        name  = "CONSOLE_HOSTED"
        value = "1"
      }
      env {
        name  = "CONSOLE_IAP_AUDIENCE"
        value = local.console_iap_audience
      }
      env {
        name  = "CONSOLE_ALLOWED_USERS"
        value = join(",", local.console_user_emails)
      }
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name  = "GOOGLE_CLOUD_LOCATION"
        value = var.region
      }
      env {
        name  = "CONSOLE_RUNTIME_ACCOUNT"
        value = local.console_runtime
      }
      env {
        name  = "CONSOLE_NETWORK_ATTACHMENT"
        value = google_compute_network_attachment.runtime.id
      }
      env {
        name  = "CONSOLE_ALLOW_DEPLOY"
        value = var.console_allow_deploy ? "1" : "0"
      }
      env {
        name  = "CONSOLE_SECURITY_REVIEW_APPROVED"
        value = var.console_security_review_approved ? "1" : "0"
      }
      env {
        name  = "CONSOLE_SANDBOX_ENGINE"
        value = var.console_sandbox_engine
      }
      env {
        name  = "CONSOLE_ALLOW_SANDBOX_TEST"
        value = var.console_sandbox_engine != "" ? "1" : "0"
      }
      env {
        name  = "CONSOLE_REQUIRE_SANDBOX_TEST"
        value = var.console_require_sandbox_test ? "1" : "0"
      }
    }
  }

  lifecycle {
    precondition {
      condition     = local.console_image != ""
      error_message = "deploy_console requires console_image (or policy_image)."
    }
  }
  depends_on = [google_project_iam_member.console, google_service_account_iam_member.console_act_as]
}

# IAP calls the service as its own service agent.
resource "google_cloud_run_v2_service_iam_member" "console_iap_invoker" {
  count    = var.deploy_console ? 1 : 0
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.console[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_project_service_identity.iap.email}"
}

# Who may sign in to the console.
resource "google_iap_web_cloud_run_service_iam_member" "console_users" {
  for_each               = var.deploy_console ? toset(var.console_users) : toset([])
  project                = var.project_id
  location               = var.region
  cloud_run_service_name = google_cloud_run_v2_service.console[0].name
  role                   = "roles/iap.httpsResourceAccessor"
  member                 = each.value
}
