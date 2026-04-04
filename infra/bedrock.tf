# Bedrock Knowledge Base data source — connects existing KB to S3 bucket's jds/ prefix.
#
# The KB is created manually in the console with Titan Embed v2 (see SETUP.md Step 3.2.5)
# as the embedding model. This resource wires the S3 data source so Bedrock
# automatically syncs and indexes JD documents for RAG retrieval.

resource "aws_bedrockagent_data_source" "jds" {
  knowledge_base_id = var.bedrock_kb_id
  name              = "${var.project_name}-jds"

  data_source_configuration {
    type = "S3"
    s3_configuration {
      bucket_arn         = "arn:aws:s3:::${var.s3_bucket_name}"
      inclusion_prefixes = ["jds/"]
    }
  }
}
