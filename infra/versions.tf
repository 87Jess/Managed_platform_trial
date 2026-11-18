terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 6.10, < 7"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = ">= 6.10, < 7"
    }
  }
  # Configure an encrypted, access-controlled remote backend before any real apply, e.g.
  # backend "gcs" { bucket = "REPLACE-tfstate-bucket", prefix = "managed-agents" }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

provider "google-beta" {
  project = var.project_id
  region  = var.region
}
