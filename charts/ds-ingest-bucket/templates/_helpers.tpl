{{/*
Helpers for ds-ingest-bucket. ARNs are built from aws.accountId, aws.region
and the names in values rather than read from ACK resource status: Helm
renders every template before any resource exists, and the names are fixed,
so the ARNs are known up front.
*/}}

{{- define "ds-ingest-bucket.labels" -}}
app.kubernetes.io/name: ds-ingest-bucket
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- with .Values.commonLabels }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{/* ACK annotations every resource carries: the region it lives in. */}}
{{- define "ds-ingest-bucket.ackAnnotations" -}}
services.k8s.aws/region: {{ .Values.aws.region }}
{{- end }}

{{/* ACK tag list ([{key, value}]) for buckets and IAM users. */}}
{{- define "ds-ingest-bucket.tagSet" -}}
{{- $tags := list (dict "key" "app.kubernetes.io/managed-by" "value" "ds-ingest-bucket") }}
{{- range $k, $v := .Values.tags }}
{{- $tags = append $tags (dict "key" $k "value" (toString $v)) }}
{{- end }}
{{- toYaml $tags }}
{{- end }}

{{/* ACK tag map ({key: value}) for queues. */}}
{{- define "ds-ingest-bucket.tagMap" -}}
{{- $tags := dict "app.kubernetes.io/managed-by" "ds-ingest-bucket" }}
{{- range $k, $v := .Values.tags }}
{{- $_ := set $tags $k (toString $v) }}
{{- end }}
{{- toYaml $tags }}
{{- end }}

{{- define "ds-ingest-bucket.logBucketName" -}}
{{- .Values.accessLogs.bucketName | default (printf "%s-logs" .Values.bucket.name) }}
{{- end }}

{{- define "ds-ingest-bucket.bucketArn" -}}
{{- printf "arn:%s:s3:::%s" .Values.aws.partition .Values.bucket.name }}
{{- end }}

{{- define "ds-ingest-bucket.logBucketArn" -}}
{{- printf "arn:%s:s3:::%s" .Values.aws.partition (include "ds-ingest-bucket.logBucketName" .) }}
{{- end }}

{{/* ARN of an IAM user under iam.path. Call with (dict "root" $ "name" ...). */}}
{{- define "ds-ingest-bucket.userArn" -}}
{{- printf "arn:%s:iam::%s:user%s%s" .root.Values.aws.partition .root.Values.aws.accountId .root.Values.iam.path .name }}
{{- end }}

{{/* ARN of an SQS queue in this account and region. Call with (dict "root" $ "name" ...). */}}
{{- define "ds-ingest-bucket.queueArn" -}}
{{- printf "arn:%s:sqs:%s:%s:%s" .root.Values.aws.partition .root.Values.aws.region .root.Values.aws.accountId .name }}
{{- end }}

{{/*
A Deny statement for any request not over TLS 1.2 or later, on the given
bucket ARN and its objects.
*/}}
{{- define "ds-ingest-bucket.denyInsecureTransport" -}}
{{- $arn := . }}
{{- $resources := list $arn (printf "%s/*" $arn) }}
{{- list
  (dict "Sid" "DenyInsecureTransport" "Effect" "Deny" "Principal" "*" "Action" "s3:*" "Resource" $resources
    "Condition" (dict "Bool" (dict "aws:SecureTransport" "false")))
  (dict "Sid" "DenyTlsBelow12" "Effect" "Deny" "Principal" "*" "Action" "s3:*" "Resource" $resources
    "Condition" (dict "NumericLessThan" (dict "s3:TlsVersion" "1.2")))
  | toJson }}
{{- end }}

{{/*
Render-time checks. Every template that renders when enabled includes this,
so a bad value fails `helm template` whichever file is rendered first.
*/}}
{{- define "ds-ingest-bucket.validate" -}}
{{- $v := .Values }}
{{- if not (regexMatch "^[0-9]{12}$" (toString $v.aws.accountId)) }}
{{- fail "aws.accountId must be the 12-digit AWS account id" }}
{{- end }}
{{- if not (regexMatch "^[a-z]{2}(-[a-z]+)+-[0-9]$" $v.aws.region) }}
{{- fail (printf "aws.region %q does not look like an AWS region" $v.aws.region) }}
{{- end }}
{{- if not (regexMatch "^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$" $v.bucket.name) }}
{{- fail "bucket.name must be 3-63 lowercase letters, digits or hyphens (no dots), starting and ending with a letter or digit" }}
{{- end }}
{{- if and $v.accessLogs.enabled (not (regexMatch "^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$" (include "ds-ingest-bucket.logBucketName" .))) }}
{{- fail "the access-log bucket name (accessLogs.bucketName, or bucket.name + \"-logs\") must be 3-63 lowercase letters, digits or hyphens" }}
{{- end }}
{{- if not (and (hasSuffix "/" $v.bucket.deliveryPrefix) (not (hasPrefix "/" $v.bucket.deliveryPrefix)) (gt (len $v.bucket.deliveryPrefix) 1)) }}
{{- fail "bucket.deliveryPrefix must be non-empty, end in / and not start with /" }}
{{- end }}
{{- if contains "*" $v.bucket.deliveryPrefix }}
{{- fail "bucket.deliveryPrefix must not contain *" }}
{{- end }}
{{- if not (has $v.bucket.deletionPolicy (list "retain" "delete")) }}
{{- fail "bucket.deletionPolicy must be retain or delete" }}
{{- end }}
{{- if not (has $v.bucket.versioning (list "Enabled" "Suspended")) }}
{{- fail "bucket.versioning must be Enabled or Suspended" }}
{{- end }}
{{- if not (has $v.bucket.encryption.sseAlgorithm (list "AES256" "aws:kms")) }}
{{- fail "bucket.encryption.sseAlgorithm must be AES256 or aws:kms" }}
{{- end }}
{{- if and (eq $v.bucket.encryption.sseAlgorithm "aws:kms") (not $v.bucket.encryption.kmsKeyId) }}
{{- fail "bucket.encryption.kmsKeyId is required with aws:kms" }}
{{- end }}
{{- range $k := list "currentExpirationDays" "noncurrentExpirationDays" "abortIncompleteMultipartUploadDays" }}
{{- if lt (int (index $v.bucket.lifecycle $k)) 1 }}
{{- fail (printf "bucket.lifecycle.%s must be at least 1" $k) }}
{{- end }}
{{- end }}
{{- if and $v.accessLogs.enabled (or (not (hasSuffix "/" $v.accessLogs.targetPrefix)) (hasPrefix "/" $v.accessLogs.targetPrefix)) }}
{{- fail "accessLogs.targetPrefix must end in / and not start with /" }}
{{- end }}
{{- if not (regexMatch "^/([A-Za-z0-9+=,.@_-]+/)+$" $v.iam.path) }}
{{- fail "iam.path must start and end with / (e.g. /ds-ingest/)" }}
{{- end }}
{{- if not (regexMatch "^arn:[a-z-]+:iam::[0-9]{12}:policy/.+$" $v.iam.permissionsBoundaryArn) }}
{{- fail "iam.permissionsBoundaryArn must be the ARN of the boundary policy the bootstrap step created" }}
{{- end }}
{{- if not (has $v.writer.mode (list "iamUser" "crossAccount")) }}
{{- fail "writer.mode must be iamUser or crossAccount" }}
{{- end }}
{{- if eq $v.writer.mode "crossAccount" }}
{{- if not $v.writer.principalArns }}
{{- fail "writer.principalArns is required in crossAccount mode" }}
{{- end }}
{{- range $v.writer.principalArns }}
{{- if not (regexMatch "^arn:[a-z-]+:iam::[0-9]{12}:(root|role/.+|user/.+)$" .) }}
{{- fail (printf "writer.principalArns: %q is not an IAM account, role or user ARN" .) }}
{{- end }}
{{- end }}
{{- end }}
{{- range $v.reader.allowedSourceCidrs }}
{{- if not (regexMatch "^[0-9a-fA-F:.]+/[0-9]{1,3}$" .) }}
{{- fail (printf "reader.allowedSourceCidrs: %q is not a CIDR" .) }}
{{- end }}
{{- end }}
{{- if $v.notifications.sqs.enabled }}
{{- $q := $v.notifications.sqs }}
{{- if or (lt (int $q.receiveWaitTimeSeconds) 0) (gt (int $q.receiveWaitTimeSeconds) 20) }}
{{- fail "notifications.sqs.receiveWaitTimeSeconds must be 0-20" }}
{{- end }}
{{- if or (lt (int $q.visibilityTimeoutSeconds) 300) (gt (int $q.visibilityTimeoutSeconds) 43200) }}
{{- fail "notifications.sqs.visibilityTimeoutSeconds must be 300-43200 (an ingest takes 1-2 minutes)" }}
{{- end }}
{{- if lt (int $q.maxReceiveCount) 1 }}
{{- fail "notifications.sqs.maxReceiveCount must be at least 1" }}
{{- end }}
{{- if not $q.events }}
{{- fail "notifications.sqs.events must not be empty" }}
{{- end }}
{{- end }}
{{- end }}
