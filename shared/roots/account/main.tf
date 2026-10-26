terraform {
  required_providers {
    databricks = {
      source  = "databricks/databricks"
      version = "~> 1.111.0"
    }
  }
  required_version = ">= 1.0"

  backend "local" {}
}

provider "databricks" {
  alias         = "account"
  host          = var.databricks_account_host
  account_id    = var.databricks_account_id
  client_id     = var.databricks_client_id
  client_secret = var.databricks_client_secret
  auth_type     = local.databricks_auth_type
}

provider "databricks" {
  alias         = "workspace"
  host          = var.databricks_workspace_host
  client_id     = var.databricks_client_id
  client_secret = var.databricks_client_secret
  auth_type     = local.databricks_auth_type
}

variable "databricks_account_host" {
  type    = string
  default = "https://accounts.cloud.databricks.com"
}

variable "databricks_account_id" {
  type = string
}

variable "databricks_client_id" {
  type = string
}

variable "databricks_client_secret" {
  type      = string
  sensitive = true
}

# Set only by scripts/terraform_layer.sh, from the DATABRICKS_AUTH_TYPE that
# scripts/terraform_env.sh kept in a pipeline job (github-oidc or
# azure-devops-oidc); it refuses any caller's value.
variable "databricks_pipeline_auth_type" {
  type    = string
  default = ""
  validation {
    condition     = contains(["", "github-oidc", "azure-devops-oidc"], var.databricks_pipeline_auth_type)
    error_message = "databricks_pipeline_auth_type is github-oidc, azure-devops-oidc or empty."
  }
}

locals {
  # The env's client secret pins OAuth M2M, so ARM_* kept for the azurerm
  # backend (scripts/terraform_env.sh) can't add Azure auth ("more than one
  # authorization method") or replace it. An IDs-only auth file in a pipeline
  # job uses the job's OIDC; null leaves the provider's own choice.
  databricks_auth_type = (var.databricks_pipeline_auth_type != "" ? var.databricks_pipeline_auth_type
  : var.databricks_client_secret != "" ? "oauth-m2m" : null)
}

variable "databricks_workspace_id" {
  type    = string
  default = ""
}

variable "serverless_usage_policy_id" {
  type    = string
  default = ""
}

variable "databricks_workspace_host" {
  type    = string
  default = ""
}

variable "uc_catalog" {
  type    = string
  default = ""
}

variable "uc_tables" {
  type    = list(string)
  default = []
}

variable "manage_groups" {
  type    = bool
  default = false
}
variable "groups" {
  type = map(object({
    description = optional(string, "")
  }))
  default = {}
}
variable "group_members" {
  type    = map(list(string))
  default = {}
}
variable "tag_policies" {
  type = list(object({
    key         = string
    description = optional(string, "")
    values      = list(string)
  }))
  default = []
}

variable "tag_assignments" {
  type = list(object({
    entity_type = string
    entity_name = string
    tag_key     = string
    tag_value   = string
  }))
  default = []
}
variable "fgac_policies" {
  type = list(object({
    name              = string
    policy_type       = string
    catalog           = string
    to_principals     = list(string)
    except_principals = optional(list(string), [])
    comment           = optional(string, "")
    match_condition   = optional(string)
    match_alias       = optional(string)
    function_name     = string
    function_catalog  = string
    function_schema   = string
    when_condition    = optional(string)
  }))
  default = []
}
variable "sql_warehouse_id" {
  type    = string
  default = ""
}

variable "warehouse_name" {
  type    = string
  default = "ABAC Serverless Warehouse"
}

variable "genie_space_id" {
  type    = string
  default = ""
}

variable "genie_space_title" {
  type    = string
  default = "Genie Space"
}

variable "genie_space_description" {
  type    = string
  default = ""
}

variable "genie_sample_questions" {
  type    = list(string)
  default = []
}

variable "genie_instructions" {
  type    = string
  default = ""
}
variable "genie_benchmarks" {
  type = list(object({
    question = string
    sql      = string
  }))
  default = []
}
variable "genie_sql_filters" {
  type = list(object({
    sql          = string
    display_name = string
    comment      = string
    instruction  = string
  }))
  default = []
}
variable "genie_sql_expressions" {
  type = list(object({
    alias        = string
    sql          = string
    display_name = string
    comment      = string
    instruction  = string
  }))
  default = []
}
variable "genie_sql_measures" {
  type = list(object({
    alias        = string
    sql          = string
    display_name = string
    comment      = string
    instruction  = string
  }))
  default = []
}
variable "genie_join_specs" {
  type = list(object({
    left_table  = string
    left_alias  = string
    right_table = string
    right_alias = string
    sql         = string
    comment     = string
    instruction = string
  }))
  default = []
}

module "account" {
  source = "../../modules/account"

  providers = {
    databricks.account   = databricks.account
    databricks.workspace = databricks.workspace
  }

  manage_groups = var.manage_groups
  groups        = var.groups
  group_members = var.group_members
  tag_policies  = var.tag_policies
}

output "group_ids" {
  value = module.account.group_ids
}

output "group_names" {
  value = module.account.group_names
}

output "tag_policy_keys" {
  value = module.account.tag_policy_keys
}
