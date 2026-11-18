variable "project_id" {
  type    = string
  default = "itsai-mai-gcp-lab-proj"
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "service_subnet_cidr" {
  description = "Subnet used by Cloud Run direct VPC egress (Kong + policy broker)."
  type        = string
  default     = "10.40.0.0/24"
}

variable "psc_subnet_cidr" {
  description = "Subnet for the Agent Engine PSC interface network attachment."
  type        = string
  default     = "10.40.1.0/28"
}

variable "registry_file" {
  description = "Agent registry; one dedicated runtime service account is created per agent."
  type        = string
  default     = "../registry/agents.json"
}

variable "deploy_workloads" {
  description = "Create the Kong + policy broker Cloud Run service. Keep false until the Stage 2 gates are met."
  type        = bool
  default     = false
}

variable "policy_image" {
  description = "Policy broker image, pinned by digest, built by cloudbuild.yaml."
  type        = string
  default     = ""
}

variable "kong_image" {
  description = "Kong gateway image, pinned by digest, built by cloudbuild.yaml."
  type        = string
  default     = ""
}

variable "entra_tenant_id" {
  type    = string
  default = ""
}

variable "entra_audience" {
  type    = string
  default = ""
}
