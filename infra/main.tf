locals {
  registry  = jsondecode(file(var.registry_file))
  agent_ids = toset([for agent in local.registry.agents : agent.id])
  prefix    = "${var.project_id}-platform"
  apis = toset([
    "aiplatform.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "compute.googleapis.com",
    "dns.googleapis.com",
    "iam.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "storage.googleapis.com",
  ])
  restricted_vip = "199.36.153.4/30"
}

data "google_project" "this" {}

resource "google_project_service" "apis" {
  for_each           = local.apis
  service            = each.value
  disable_on_destroy = false
}

# ----------------------------------------------------------------- Network

resource "google_compute_network" "platform" {
  name                            = "managed-agents"
  auto_create_subnetworks         = false
  delete_default_routes_on_create = true # no default internet route
  depends_on                      = [google_project_service.apis]
}

resource "google_compute_subnetwork" "service" {
  name                     = "managed-agents-service"
  network                  = google_compute_network.platform.id
  region                   = var.region
  ip_cidr_range            = var.service_subnet_cidr
  private_ip_google_access = true
}

resource "google_compute_subnetwork" "psc" {
  name                     = "managed-agents-psc"
  network                  = google_compute_network.platform.id
  region                   = var.region
  ip_cidr_range            = var.psc_subnet_cidr
  private_ip_google_access = true
}

# Agent Engine joins the VPC through a PSC interface on this attachment.
resource "google_compute_network_attachment" "runtime" {
  name                  = "managed-agent-runtime"
  region                = var.region
  connection_preference = "ACCEPT_MANUAL"
  subnetworks           = [google_compute_subnetwork.psc.self_link]
  producer_accept_lists = [data.google_project.this.project_id]
}

resource "google_compute_route" "restricted_apis" {
  name             = "restricted-googleapis"
  network          = google_compute_network.platform.name
  dest_range       = local.restricted_vip
  next_hop_gateway = "default-internet-gateway"
  priority         = 100
}

resource "google_compute_firewall" "allow_restricted_apis" {
  name               = "allow-egress-restricted-googleapis"
  network            = google_compute_network.platform.name
  direction          = "EGRESS"
  priority           = 100
  destination_ranges = [local.restricted_vip]
  allow {
    protocol = "tcp"
    ports    = ["443"]
  }
}

resource "google_compute_firewall" "allow_internal_https" {
  name               = "allow-egress-internal-https"
  network            = google_compute_network.platform.name
  direction          = "EGRESS"
  priority           = 200
  destination_ranges = [var.service_subnet_cidr]
  allow {
    protocol = "tcp"
    ports    = ["443"]
  }
}

resource "google_compute_firewall" "deny_all_egress" {
  name               = "deny-all-egress"
  network            = google_compute_network.platform.name
  direction          = "EGRESS"
  priority           = 65000
  destination_ranges = ["0.0.0.0/0"]
  deny {
    protocol = "all"
  }
}

resource "google_dns_managed_zone" "googleapis" {
  name       = "googleapis-restricted"
  dns_name   = "googleapis.com."
  visibility = "private"
  private_visibility_config {
    networks {
      network_url = google_compute_network.platform.id
    }
  }
  depends_on = [google_project_service.apis]
}

resource "google_dns_record_set" "restricted_a" {
  managed_zone = google_dns_managed_zone.googleapis.name
  name         = "restricted.googleapis.com."
  type         = "A"
  ttl          = 300
  rrdatas      = ["199.36.153.4", "199.36.153.5", "199.36.153.6", "199.36.153.7"]
}

resource "google_dns_record_set" "wildcard_cname" {
  managed_zone = google_dns_managed_zone.googleapis.name
  name         = "*.googleapis.com."
  type         = "CNAME"
  ttl          = 300
  rrdatas      = ["restricted.googleapis.com."]
}

# ----------------------------------------------------------------- Storage, images, secrets

resource "google_storage_bucket" "buckets" {
  for_each                    = toset(["registry", "staging", "documents"])
  name                        = "${local.prefix}-${each.value}"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  versioning {
    enabled = true
  }
  depends_on = [google_project_service.apis]
}

resource "google_storage_bucket_object" "registry" {
  bucket = google_storage_bucket.buckets["registry"].name
  name   = "agents.json"
  source = var.registry_file
  lifecycle {
    ignore_changes = all # Terraform only bootstraps the registry; operators manage it afterwards
  }
}

resource "google_artifact_registry_repository" "images" {
  repository_id = "managed-agents"
  location      = var.region
  format        = "DOCKER"
  docker_config {
    immutable_tags = true
  }
  depends_on = [google_project_service.apis]
}

# Secret containers only; versions are added out of band, never through Terraform state.
resource "google_secret_manager_secret" "secrets" {
  for_each  = toset(["app-token-sha256", "entra-jwks"])
  secret_id = "managed-agents-${each.value}"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

# ----------------------------------------------------------------- Identities

resource "google_service_account" "build" {
  account_id   = "platform-build"
  display_name = "Managed agents: Cloud Build"
}

resource "google_service_account" "broker" {
  account_id   = "platform-broker"
  display_name = "Managed agents: policy broker"
}

# One dedicated runtime identity per registry agent. Deliberately no model or data grants:
# agents reach models and data only through the gateway.
resource "google_service_account" "agents" {
  for_each     = local.agent_ids
  account_id   = "agent-${each.value}"
  display_name = "Managed agent runtime: ${each.value}"
}

resource "google_project_iam_member" "build_logs" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = google_service_account.build.member
}

resource "google_artifact_registry_repository_iam_member" "build_push" {
  repository = google_artifact_registry_repository.images.name
  location   = var.region
  role       = "roles/artifactregistry.writer"
  member     = google_service_account.build.member
}

resource "google_project_iam_member" "broker_vertex" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = google_service_account.broker.member
}

resource "google_storage_bucket_iam_member" "broker_registry" {
  bucket = google_storage_bucket.buckets["registry"].name
  role   = "roles/storage.objectViewer"
  member = google_service_account.broker.member
}

resource "google_storage_bucket_iam_member" "broker_documents" {
  bucket = google_storage_bucket.buckets["documents"].name
  role   = "roles/storage.objectViewer"
  member = google_service_account.broker.member
  condition {
    title      = "approved-prefix-only"
    expression = "resource.name.startsWith(\"projects/_/buckets/${local.prefix}-documents/objects/approved/\")"
  }
}

resource "google_secret_manager_secret_iam_member" "broker" {
  for_each  = google_secret_manager_secret.secrets
  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.broker.member
}

# The Agent Platform service agent manages the PSC interface and DNS peering.
resource "google_project_service_identity" "aiplatform" {
  provider   = google-beta
  service    = "aiplatform.googleapis.com"
  depends_on = [google_project_service.apis]
}

resource "google_project_iam_member" "aiplatform_agent" {
  for_each = toset(["roles/compute.networkAdmin", "roles/dns.peer"])
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_project_service_identity.aiplatform.email}"
}

# ----------------------------------------------------------------- Gateway workload (off by default)

resource "google_cloud_run_v2_service" "gateway" {
  count               = var.deploy_workloads ? 1 : 0
  name                = "managed-agents-gateway"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"
  deletion_protection = true

  template {
    service_account = google_service_account.broker.email
    scaling {
      min_instance_count = 0
      max_instance_count = 3
    }
    vpc_access {
      egress = "ALL_TRAFFIC"
      network_interfaces {
        network    = google_compute_network.platform.id
        subnetwork = google_compute_subnetwork.service.id
      }
    }

    containers {
      name       = "kong"
      image      = var.kong_image
      depends_on = ["policy"]
      ports {
        container_port = 8080
      }
      resources {
        limits = { cpu = "1", memory = "512Mi" }
      }
    }

    containers {
      name  = "policy"
      image = var.policy_image
      env {
        name  = "HOST"
        value = "127.0.0.1"
      }
      env {
        name  = "PORT"
        value = "8081"
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
        name  = "ENTRA_TENANT_ID"
        value = var.entra_tenant_id
      }
      env {
        name  = "ENTRA_AUDIENCE"
        value = var.entra_audience
      }
      env {
        name  = "ENTRA_JWKS_PATH"
        value = "/secrets/entra/jwks.json"
      }
      env {
        name = "APP_TOKEN_SHA256"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.secrets["app-token-sha256"].secret_id
            version = "latest"
          }
        }
      }
      volume_mounts {
        name       = "entra-jwks"
        mount_path = "/secrets/entra"
      }
      startup_probe {
        http_get {
          path = "/healthz"
          port = 8081
        }
      }
      resources {
        limits = { cpu = "1", memory = "512Mi" }
      }
    }

    volumes {
      name = "entra-jwks"
      secret {
        secret = google_secret_manager_secret.secrets["entra-jwks"].secret_id
        items {
          version = "latest"
          path    = "jwks.json"
        }
      }
    }
  }

  lifecycle {
    precondition {
      condition = alltrue([var.policy_image != "", var.kong_image != "",
      var.entra_tenant_id != "", var.entra_audience != ""])
      error_message = "deploy_workloads requires policy_image, kong_image, entra_tenant_id and entra_audience."
    }
  }
  depends_on = [google_secret_manager_secret_iam_member.broker]
}
