output "network_attachment" {
  description = "Use as CONSOLE_NETWORK_ATTACHMENT and deploy_agent --network-attachment."
  value       = google_compute_network_attachment.runtime.id
}

output "staging_bucket" {
  value = "gs://${google_storage_bucket.buckets["staging"].name}"
}

output "agent_service_accounts" {
  description = "Dedicated runtime identity per agent; use one as CONSOLE_RUNTIME_ACCOUNT."
  value       = { for id, account in google_service_account.agents : id => account.email }
}

output "image_repository" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}

output "gateway_uri" {
  value = var.deploy_workloads ? google_cloud_run_v2_service.gateway[0].uri : null
}
