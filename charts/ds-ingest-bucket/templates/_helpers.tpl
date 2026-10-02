{{/*
Helpers for ds-ingest-bucket. Names and resource paths are built from the
values rather than read from managed-resource status: Helm renders every
template before anything exists in GCP, and the names are fixed, so they are
known up front.
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

{{/* GCP labels for buckets, the topic and the subscription. */}}
{{- define "ds-ingest-bucket.gcpLabels" -}}
{{- $l := dict "managed-by" "ds-ingest-bucket" "purpose" "schedule-feed-ingest" }}
{{- range $k, $v := .Values.labels }}
{{- $_ := set $l $k (toString $v) }}
{{- end }}
{{- toYaml $l }}
{{- end }}

{{/*
The spec fields every managed resource except the buckets uses: the provider
config and full management. IAM bindings must be fully managed so that
removing a member from the values really revokes it in GCP; an orphaned
binding would keep granting access.
*/}}
{{- define "ds-ingest-bucket.managed" -}}
providerConfigRef:
  kind: {{ .Values.crossplane.providerConfigRef.kind }}
  name: {{ .Values.crossplane.providerConfigRef.name }}
managementPolicies: ["*"]
{{- end }}

{{/*
The same for the two buckets, honouring crossplane.deletionPolicy: without
`Delete`, removing the resource orphans the bucket and its data in GCP.
*/}}
{{- define "ds-ingest-bucket.managedBucket" -}}
providerConfigRef:
  kind: {{ .Values.crossplane.providerConfigRef.kind }}
  name: {{ .Values.crossplane.providerConfigRef.name }}
{{- if eq .Values.crossplane.deletionPolicy "Orphan" }}
managementPolicies: ["Observe", "Create", "Update", "LateInitialize"]
{{- else }}
managementPolicies: ["*"]
{{- end }}
{{- end }}

{{- define "ds-ingest-bucket.auditBucketName" -}}
{{- .Values.auditLogs.bucket.name | default (printf "%s-audit" .Values.bucket.name) }}
{{- end }}

{{- define "ds-ingest-bucket.deleteRoleName" -}}
{{- printf "projects/%s/roles/%s" .Values.gcp.projectId .Values.reader.deleteRole.roleId }}
{{- end }}

{{- define "ds-ingest-bucket.topicPath" -}}
{{- printf "projects/%s/topics/%s" .Values.gcp.projectId .Values.notifications.pubsub.topicName }}
{{- end }}

{{/*
The bucket-privacy settings both buckets share: uniform bucket-level access
(IAM only, no object ACLs), public access prevention enforced (allUsers and
allAuthenticatedUsers can never be granted), no object retention (so the
publisher's storage.objects.setRetention can't lock anything), and no
force-destroy.
*/}}
{{- define "ds-ingest-bucket.privateBucket" -}}
project: {{ .Values.gcp.projectId }}
location: {{ .Values.gcp.location }}
uniformBucketLevelAccess: true
publicAccessPrevention: enforced
enableObjectRetention: false
forceDestroy: false
{{- end }}

{{/*
Render-time checks. Every template that renders when enabled includes this,
so a bad value fails `helm template` whichever file is rendered first.
*/}}
{{- define "ds-ingest-bucket.validate" -}}
{{- $v := .Values }}
{{- $saMember := "^serviceAccount:[a-z0-9][a-z0-9-]*@[a-z0-9][a-z0-9.-]*\\.gserviceaccount\\.com$" }}
{{- $bucketName := "^[a-z0-9][a-z0-9_-]{1,61}[a-z0-9]$" }}
{{- if not (regexMatch "^[a-z][a-z0-9-]{4,28}[a-z0-9]$" (toString $v.gcp.projectId)) }}
{{- fail "gcp.projectId must be the dedicated project's id (6-30 lowercase letters, digits and hyphens)" }}
{{- end }}
{{- if not (regexMatch "^[A-Z]+(-[A-Z]+[0-9]+)?$" $v.gcp.location) }}
{{- fail (printf "gcp.location %q does not look like a Cloud Storage location (e.g. EUROPE-WEST2)" $v.gcp.location) }}
{{- end }}
{{- if not (has $v.crossplane.deletionPolicy (list "Orphan" "Delete")) }}
{{- fail "crossplane.deletionPolicy must be Orphan or Delete" }}
{{- end }}
{{- if not (regexMatch $bucketName $v.bucket.name) }}
{{- fail "bucket.name must be 3-63 lowercase letters, digits, - or _ (no dots), starting and ending with a letter or digit" }}
{{- end }}
{{- if and $v.auditLogs.bucket.enabled (not (regexMatch $bucketName (include "ds-ingest-bucket.auditBucketName" .))) }}
{{- fail "the audit-log bucket name (auditLogs.bucket.name, or bucket.name + \"-audit\") must be 3-63 lowercase letters, digits, - or _" }}
{{- end }}
{{- $sd := int $v.bucket.softDeleteRetentionDays }}
{{- if and (ne $sd 0) (or (lt $sd 7) (gt $sd 90)) }}
{{- fail "bucket.softDeleteRetentionDays must be 0 (off) or 7-90, Cloud Storage's range" }}
{{- end }}
{{- range $k := list "deleteAfterDays" "abortIncompleteMultipartUploadDays" }}
{{- if lt (int (index $v.bucket.lifecycle $k)) 1 }}
{{- fail (printf "bucket.lifecycle.%s must be at least 1" $k) }}
{{- end }}
{{- end }}
{{- with $v.bucket.encryption.defaultKmsKeyName }}
{{- if not (regexMatch "^projects/[^/]+/locations/[^/]+/keyRings/[^/]+/cryptoKeys/[^/]+$" .) }}
{{- fail "bucket.encryption.defaultKmsKeyName must be a Cloud KMS key name, or empty for Google-managed encryption" }}
{{- end }}
{{- end }}
{{- if not $v.publisher.members }}
{{- fail "publisher.members is required: the publisher's service accounts (serviceAccount:<email>), set in Ranma-Config" }}
{{- end }}
{{- range $v.publisher.members }}
{{- if not (regexMatch $saMember .) }}
{{- fail (printf "publisher.members: %q must be serviceAccount:<email> (allUsers, allAuthenticatedUsers, domain:, group: and user: are refused)" .) }}
{{- end }}
{{- end }}
{{- if not $v.publisher.roles }}
{{- fail "publisher.roles must not be empty" }}
{{- end }}
{{- $refused := list "roles/storage.admin" "roles/storage.objectAdmin" "roles/storage.legacyBucketOwner" "roles/storage.legacyObjectOwner" }}
{{- range $v.publisher.roles }}
{{- if not (regexMatch "^roles/storage\\.[A-Za-z]+$" .) }}
{{- fail (printf "publisher.roles: %q is not a predefined Cloud Storage role" .) }}
{{- end }}
{{- if has . $refused }}
{{- fail (printf "publisher.roles: %q can change IAM or bucket settings and is refused" .) }}
{{- end }}
{{- end }}
{{- if not (regexMatch $saMember $v.reader.member) }}
{{- fail "reader.member is required: schedule-ingest's service account, as serviceAccount:<email>" }}
{{- end }}
{{- if not (regexMatch "^[a-zA-Z0-9_.]{3,64}$" $v.reader.deleteRole.roleId) }}
{{- fail "reader.deleteRole.roleId must be 3-64 letters, digits, _ or ." }}
{{- end }}
{{- if has $v.reader.member $v.publisher.members }}
{{- fail "reader.member must not also be a publisher member" }}
{{- end }}
{{- with $v.auditLogs.sinkWriterIdentity }}
{{- if not (regexMatch $saMember .) }}
{{- fail "auditLogs.sinkWriterIdentity must be serviceAccount:<email> (the sink's writer identity)" }}
{{- end }}
{{- end }}
{{- if $v.usageAlerts.enabled }}
{{- if not $v.usageAlerts.notificationChannels }}
{{- fail "usageAlerts.notificationChannels is required with usageAlerts.enabled" }}
{{- end }}
{{- range $v.usageAlerts.notificationChannels }}
{{- if not (regexMatch "^projects/[^/]+/notificationChannels/[0-9]+$" .) }}
{{- fail (printf "usageAlerts.notificationChannels: %q is not a notification channel name" .) }}
{{- end }}
{{- end }}
{{- end }}
{{- if $v.notifications.pubsub.enabled }}
{{- if not (regexMatch "^[0-9]{6,20}$" (toString $v.gcp.projectNumber)) }}
{{- fail "gcp.projectNumber is required with notifications.pubsub.enabled (it names the Cloud Storage service agent that publishes)" }}
{{- end }}
{{- $ack := int $v.notifications.pubsub.ackDeadlineSeconds }}
{{- if or (lt $ack 120) (gt $ack 600) }}
{{- fail "notifications.pubsub.ackDeadlineSeconds must be 120-600 (an ingest takes 1-2 minutes)" }}
{{- end }}
{{- end }}
{{- end }}
