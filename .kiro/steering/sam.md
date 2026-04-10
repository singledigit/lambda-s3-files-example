---
inclusion: always
---

# SAM best practices

* Use globales when possible, override at resource if needed
* Always use Arm architecture when possible
* You don't need to add the basic policies (Ex: AWSLambdaBasicDurableExecutionRolePolicy), SAM will handle this
* never try to have shared code. If required, use a layer
* Policy usage order
   * Managed policies
   * SAM policies
   * Inline policies
* Generate a samconfig.toml file
   * stack name, region, profile, should all be global
   * Build parameters, use cache and build in parallel