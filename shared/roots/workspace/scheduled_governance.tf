# ─────────────────────────────────────────────────────────────────────────────
# Scheduled steady-state governance job: moved to the data_access root
# ─────────────────────────────────────────────────────────────────────────────
# The job is deployed from shared/roots/data_access/scheduled_governance.tf, so
# governance-only envs get it too. terraform_layer.sh moves an existing job from
# this root's state into the data_access state before any command runs, so the
# job is never destroyed or recreated. If Terraform is run here directly before
# that move, this removed block makes the plan forget the job, never destroy it.

removed {
  from = databricks_job.scheduled_governance

  lifecycle {
    destroy = false
  }
}

# Declared only so the scheduled_governance_* settings in env.auto.tfvars (which
# this root also reads) raise no undeclared-variable warning; the data_access
# root declares, validates and uses them.
variable "enable_scheduled_governance" {
  type    = bool
  default = false
}

variable "scheduled_governance_mode" {
  type    = string
  default = "check"
}

variable "scheduled_governance_env" {
  type    = string
  default = "prod"
}

variable "scheduled_governance_cloud" {
  type    = string
  default = "aws"
}

variable "scheduled_governance_catalog" {
  type    = string
  default = ""
}

variable "scheduled_governance_config_source" {
  type    = string
  default = ""
}

variable "scheduled_governance_cron" {
  type    = string
  default = "0 0 6 * * ?"
}

variable "scheduled_governance_timezone" {
  type    = string
  default = "UTC"
}

variable "scheduled_governance_git_url" {
  type    = string
  default = ""
}

variable "scheduled_governance_git_branch" {
  type    = string
  default = "main"
}

variable "scheduled_governance_git_provider" {
  type    = string
  default = "gitHub"
}

variable "scheduled_governance_serverless_client" {
  type    = string
  default = "2"
}

variable "scheduled_governance_dependencies" {
  type    = list(string)
  default = ["python-hcl2", "databricks-sdk", "pyyaml", "requests"]
}

variable "scheduled_governance_notification_emails" {
  type    = list(string)
  default = []
}

variable "scheduled_governance_job_name" {
  type    = string
  default = ""
}
