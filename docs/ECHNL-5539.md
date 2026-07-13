# ECHNL-5539 数据迁移及文档迁移脚本 Review

## 背景

本次需求将 Account Middle Office > Account Opening > Agreements & Documents 的上传接口参数由：

```text
account_opening.file_path_config.ao_upload
```

调整为：

```text
account_opening.file_path_config.upload
```

迁移脚本目标是：

- 判断站点是否开启 `account_opening` 模块。
- 将开户协议书文件从旧配置目录迁移到新配置目录。
- 更新 MongoDB `site_file_field_data.upload_type`。
- 更新 MySQL `file_managed.uri`。
- 通过 `drush php:script ${dirname}/data/MigratAOAgreement#ECHNL-5539.php` 执行。
- BIZ 层调整 `isValidDocumentUploadFile()` 的上传文档判断逻辑。

## 总体结论

当前迁移脚本不建议直接上 UAT。

主要风险是脚本按旧目录和旧 `upload_type` 全量迁移，没有先限定“开户协议书/Agreements & Documents 实际引用的 `site_file.entity_id`”。这会带来误迁移、误改 DB、文件覆盖、DB 指向不存在文件等风险。

建议改成“精确 fid 清单驱动”的迁移：

1. 从 `documents` / `documents_field_data.data_value` 中解析开户协议书实际引用的 `site_file.entity_id`。
2. 根据 `site_file_field_data.entity_id` 找 typed file field，例如 `field_pdf.target_id` / `field_image.target_id` / `field_document.fid`。
3. 只迁移这些 typed fid 对应的 `file_managed` 记录和物理文件。
4. 先复制文件并校验成功，再更新 MongoDB 和 MySQL。
5. 每个文件单独记录 success / skip / error，脚本应可安全重跑。

## Critical Findings

### 1. 先更新 DB，再复制实际文件

脚本当前顺序是：

1. update `site_file_field_data.upload_type`
2. update `file_managed.uri`
3. 最后 `copyDir($oldRealDir, $newRealDir, $backDir)`

风险：

- 如果文件复制失败、权限不足、磁盘满、source 文件不存在，DB 已经指向新路径。
- 文件流接口会按新的 `file_managed.uri` 取文件，最终返回 404 / 空文件 / fallback 到其他文件。

建议：

- 必须先复制文件，并用 `file_exists()` / size / hash 校验目标文件存在。
- 复制成功后再更新 `file_managed.uri` 和 `site_file_field_data.upload_type`。
- 每个文件逐个处理，避免一个失败影响全批。

### 2. 迁移范围过大，可能误迁移非协议书文件

脚本当前 Mongo 查询：

```php
$mongodb->selectCollection('site_file_field_data')->find(
  ['upload_type' => 'account_opening.file_path_config.ao_upload'],
  ['projection' => ['entity_id' => 1]]
)
```

脚本当前 MySQL 查询：

```php
$database->select('file_managed', 'fm')
  ->fields('fm', ['fid', 'uri'])
  ->condition('uri', $oldConfigDir . '%', 'LIKE')
```

风险：

- 会迁移所有使用 `ao_upload` 的文件，不保证只是开户协议书。
- 如果历史上 AO 其他功能也使用 `account_opening.file_path_config.ao_upload`，会被一起迁移。
- 如果旧目录中存在手工放置、临时文件、非 DB 引用文件，也会被复制到新目录。

建议：

- 不要按目录全量迁移。
- 不要只按 `upload_type` 全量迁移。
- 应先从 documents 业务数据中拿到协议书引用的 `site_file.entity_id`，再精确迁移这些文件。

### 3. `updateOne()` 会漏更新多语言记录

当前代码：

```php
$mongodb->selectCollection('site_file_field_data')->updateOne(
  ['entity_id' => (int) $item->entity_id],
  ['$set' => ['upload_type' => 'account_opening.file_path_config.upload']]
);
```

风险：

- `site_file_field_data` 可能同一个 `entity_id` 有多条 language row。
- `updateOne()` 只会更新其中一条。
- 最终可能出现同一个 site_file entity 一部分语言是旧 `upload_type`，一部分语言是新 `upload_type`。

建议：

```php
updateMany([
  'entity_id' => (int) $item->entity_id,
  'upload_type' => 'account_opening.file_path_config.ao_upload',
], [
  '$set' => ['upload_type' => 'account_opening.file_path_config.upload'],
]);
```

### 4. `copyDir()` 递归调用参数错误

当前代码：

```php
copyDir($sourceDir . '/' . $item, $destination . '/' . $item, $filter);
```

函数签名：

```php
function copyDir($sourceDir, $destination, $backDir, $filter = [...])
```

问题：

- 第三个参数应该是 `$backDir`，当前传入了 `$filter`。
- 子目录内如果存在同名文件，会把 array 当成 `$backDir` 使用。
- 备份目录逻辑会失效，严重时触发 warning / fatal。

最低修正：

```php
copyDir(
  $sourceDir . '/' . $item,
  $destination . '/' . $item,
  $backDir . '/' . $item,
  $filter
);
```

但更推荐不要整目录递归 copy，而是只复制 DB 精确命中的文件。

### 5. 脚本不可安全重跑

风险：

- 旧目录文件不会删除。
- 第二次执行仍会从旧目录复制到新目录。
- 如果新目录已有同名新文件，会被备份后覆盖成旧文件。

建议：

- 每条记录迁移前判断：
  - Mongo `upload_type` 是否已经是新值。
  - `file_managed.uri` 是否已经在新目录。
  - 新目标文件是否已存在且 size/hash 一致。
- 已迁移成功的记录应 skip，不重复覆盖。

## High Findings

### 6. 路径解析方式不稳

当前代码：

```php
$siteConfigPath = [
  'public:/' => Settings::get('file_public_path'),
  'private:/' => Settings::get('file_private_path'),
];
```

问题：

- Drupal URI 是 `public://` / `private://`，脚本用 `public:/` / `private:/` 做替换，不直观且容易出错。
- `Settings::get('file_public_path')` 可能是相对路径。
- 自定义 stream wrapper 或特殊站点配置时不可靠。

建议使用 Drupal stream wrapper：

```php
$wrapper = \Drupal::service('stream_wrapper_manager')->getViaUri($uri);
$realPath = $wrapper ? $wrapper->realpath() : '';
```

### 7. 未校验 old/new config 是否相同或互为子目录

当前只判断空目录：

```php
if (empty($oldRealDir) || empty($newRealDir)) {
  echo 'There are no files to be migrated';
  exit(0);
}
```

建议：

- 如果 `$oldConfigDir === $newConfigDir`，直接退出。
- 对 realpath 做规范化比较。
- 禁止 destination 位于 source 子目录内。

### 8. 未验证 `file_managed` 更新是否只作用于协议书 fid

当前：

```php
condition('uri', $oldConfigDir . '%', 'LIKE')
```

风险：

- 会改所有旧目录下的 `file_managed`。
- 不保证这些 fid 被 documents 引用。
- 不保证这些 fid 是 `site_file_field_data` typed field 引用的 fid。

建议：

- 先构建 `$targetFids`。
- MySQL 更新必须加：

```php
->condition('fid', $targetFids, 'IN')
```

并且逐个更新。

## BIZ Review

### 9. `isValidDocumentUploadFile()` 当前只允许 CMS upload type

在 `dps-tickrs` 副本中找到：

```text
D:\TTL\wvplaform\dps9\tickrs-prod-9.3.76.7\dps-tickrs\site\web\sites\default\modules\ttl_contrib_common\src\Documents\MiddleOffice\BIZ\DefaultDocumentsBizImpl.php
```

当前常量：

```php
const CMS_UPLOAD_FILE_TYPES = [
  'ttl_cms_api.file_path_config.default',
  'ttl_cms_api.file_path_config.upload',
];
```

`isValidDocumentUploadFile()` 当前逻辑：

```php
$uploadType = (string) ($fileData['upload_type'] ?? '');
if (!in_array($uploadType, self::CMS_UPLOAD_FILE_TYPES, TRUE)) {
  return FALSE;
}
```

如果开户协议书文件流仍走 common document stream API，并且迁移后 `upload_type` 变成：

```text
account_opening.file_path_config.upload
```

当前 `isValidDocumentUploadFile()` 会拒绝该文件，导致不使用实际上传文件。

建议：

```php
const DOCUMENT_UPLOAD_FILE_TYPES = [
  'ttl_cms_api.file_path_config.default',
  'ttl_cms_api.file_path_config.upload',
  'account_opening.file_path_config.ao_upload',
  'account_opening.file_path_config.upload',
];
```

同时必须继续排除 AO 客户上传附件：

```php
if (!empty($fieldOption['field_ao_config'])) {
  return FALSE;
}
```

也建议增加 document app/module 维度判断，避免所有 AO upload 都被视为合法 document upload。

### 10. `FileBizBase::UPLOAD_TYPE_MAPPING` 缺少旧类型到新类型映射

当前 `FileBizBase::UPLOAD_TYPE_MAPPING` 已存在旧类型兼容机制，但没有：

```php
'account_opening.file_path_config.ao_upload' => 'account_opening.file_path_config.upload',
```

建议加上。

原因：

- 兼容旧前端、缓存、历史配置或第三方调用。
- 即使前端已改为新 upload_type，后端也应兼容旧参数。

## 建议的安全迁移流程

### Step 1: 确认模块开启

```php
if (!\Drupal::moduleHandler()->moduleExists('account_opening')) {
  echo "account_opening module is not enabled.\n";
  return;
}
```

### Step 2: 构建开户协议书 document 引用清单

建议从 documents 业务表/集合中获取协议书 document rows，解析 `data_value` 中的 `fid`。

需要确认的数据结构：

- `documents_field_data.id`
- `documents_field_data.type`
- `documents_field_data.doc_id`
- `documents_field_data.tags`
- `documents_field_data.data_value`
- `data_value` 中的 `fid` 是 `site_file.entity_id`，不是 `file_managed.fid`

### Step 3: 根据 site_file entity id 找 typed fid

Mongo 查询：

```js
db.site_file_field_data.find({
  entity_id: { $in: [/* document data_value fid list */] },
  upload_type: "account_opening.file_path_config.ao_upload"
})
```

fid 解析优先级：

1. `field_pdf.target_id`
2. `field_image.target_id`
3. `field_document.fid`
4. `field_video.fid`
5. 最后才 fallback outer `target_id`

原则上开户协议书 PDF 应该使用 `field_pdf.target_id`。

### Step 4: 逐个复制实际文件

对每个 `file_managed.fid`：

1. 读取旧 `uri`。
2. 校验旧 `uri` 必须位于 old config dir。
3. 计算新 `uri`。
4. 校验 source 文件存在。
5. 如果 destination 存在：
   - size/hash 一致：skip copy。
   - size/hash 不一致：备份 destination，再复制。
6. 复制完成后校验 destination 存在且 size/hash 一致。

### Step 5: DB 更新

文件复制成功后再更新 MySQL：

```php
$database->update('file_managed')
  ->fields(['uri' => $newUri])
  ->condition('fid', $fileManagedFid)
  ->condition('uri', $oldUri)
  ->execute();
```

再更新 MongoDB：

```php
$mongodb->selectCollection('site_file_field_data')->updateMany([
  'entity_id' => $siteFileEntityId,
  'upload_type' => 'account_opening.file_path_config.ao_upload',
], [
  '$set' => ['upload_type' => 'account_opening.file_path_config.upload'],
]);
```

### Step 6: 输出报告

建议输出 TSV/CSV：

```text
status  site_file_entity_id  file_managed_fid  old_uri  new_uri  message
```

状态建议：

- `SUCCESS`
- `SKIP_ALREADY_MIGRATED`
- `SKIP_NOT_TARGET_DOCUMENT`
- `ERROR_SOURCE_NOT_FOUND`
- `ERROR_COPY_FAILED`
- `ERROR_DB_UPDATE_FAILED`

## 建议验证 SQL / Mongo 查询

### 迁移前检查旧 upload_type 数量

```js
db.site_file_field_data.countDocuments({
  upload_type: "account_opening.file_path_config.ao_upload"
})
```

### 迁移后检查旧 upload_type 是否仍存在

```js
db.site_file_field_data.find({
  upload_type: "account_opening.file_path_config.ao_upload"
}, {
  entity_id: 1,
  title: 1,
  upload_type: 1,
  field_pdf: 1,
  field_image: 1,
  field_document: 1
})
```

### 检查新 upload_type

```js
db.site_file_field_data.find({
  upload_type: "account_opening.file_path_config.upload"
}, {
  entity_id: 1,
  title: 1,
  upload_type: 1,
  field_pdf: 1,
  field_image: 1,
  field_document: 1
})
```

### MySQL 检查 file_managed 是否仍指向旧目录

```sql
SELECT fid, filename, uri, filemime, created, changed
FROM file_managed
WHERE uri LIKE 'OLD_CONFIG_DIR/%';
```

### MySQL 检查目标 fid 的新路径

```sql
SELECT fid, filename, uri, filemime, created, changed
FROM file_managed
WHERE fid IN (...);
```

## 上线前必须完成

1. 迁移脚本改成“document 引用清单驱动”，不要按目录或 upload_type 全量迁移。
2. 复制文件成功后再更新 DB。
3. MongoDB 更新使用 `updateMany()`，并带旧 `upload_type` 条件。
4. 修复 `copyDir()` 参数错误，或移除 `copyDir()` 改成逐文件复制。
5. `FileBizBase::UPLOAD_TYPE_MAPPING` 增加旧 upload_type 到新 upload_type 的映射。
6. `isValidDocumentUploadFile()` 如需支持开户协议书，必须允许 `account_opening.file_path_config.upload`，但同时继续排除 `field_ao_config` 用户上传附件。
7. 输出迁移报告，并支持重跑幂等。

## 2026-06-25 补充确认：DefaultDocumentsBizImpl.php

用户提供文件：

```text
D:\TTL\code-review\ECHNL-5539\DefaultDocumentsBizImpl.php
```

### 对原 concern 的回应

原 concern：

> `isValidDocumentUploadFile()` 会拒绝 `account_opening.file_path_config.upload`。如果开户协议书 stream 仍走 common document API，这个改动会导致上传文件不被使用。

根据用户提供的代码，此 concern 已经被部分修复：

```php
const CMS_UPLOAD_FILE_TYPES = [
  'ttl_cms_api.file_path_config.default',
  'ttl_cms_api.file_path_config.upload',
  'account_opening.file_path_config.upload',
];
```

因此，`account_opening.file_path_config.upload` 不会再因为 `upload_type` 被直接拒绝。

### 仍需修正的问题

#### 1. account_opening 目录配置 key 写错

当前代码：

```php
$accountOpeningExpectedUploadDir = (string) $this->svConfig
  ->getAppConfig('account_opening', 'file_path_config')
  ->get('account_opening');
```

但本需求和现有 app_config 中的 key 是：

```yaml
file_path_config:
  upload: 'private://amo/account-opening/upload'
```

所以这里应改为：

```php
$accountOpeningExpectedUploadDir = (string) $this->svConfig
  ->getAppConfig('account_opening', 'file_path_config')
  ->get('upload');
```

否则 `$accountOpeningExpectedUploadDir` 可能为空。

#### 2. 空配置时 `return TRUE` 会绕过目录校验

当前代码：

```php
if ($cmsExpectedUploadDir === '' || $accountOpeningExpectedUploadDir === '') {
  return TRUE;
}
```

风险：

- 如果 `get('account_opening')` 返回空，会直接 `return TRUE`。
- 目录校验会被完全绕过。
- 只要 `upload_type` 在 allowlist、无 `field_ao_config`、`con_id` 匹配，就会通过，即使 `file_uri` 不在预期目录。

建议改成只使用非空目录构建 allowlist，并且没有任何可校验目录时返回 `FALSE`：

```php
$expectedUploadDirs = array_filter([
  (string) $this->svConfig
    ->getAppConfig('ttl_cms_api', 'file_path_config')
    ->get('upload'),
  (string) $this->svConfig
    ->getAppConfig('account_opening', 'file_path_config')
    ->get('upload'),
]);

if (empty($expectedUploadDirs)) {
  return FALSE;
}

$fileUri = (string) $fileData['file_uri'];
foreach ($expectedUploadDirs as $expectedUploadDir) {
  $expectedUploadDir = rtrim($expectedUploadDir, '/');
  if ($fileUri === $expectedUploadDir || str_starts_with($fileUri, $expectedUploadDir . '/')) {
    return TRUE;
  }
}

return FALSE;
```

#### 3. 是否兼容旧 `ao_upload`

当前 allowlist 只加入了新类型：

```php
'account_opening.file_path_config.upload',
```

如果迁移期间仍存在旧数据：

```php
account_opening.file_path_config.ao_upload
```

则旧数据仍会被拒绝。

建议二选一：

- 如果保证迁移先完成且无旧数据：可以不加旧类型。
- 如果希望灰度/回滚/兼容历史数据：临时加入旧类型：

```php
'account_opening.file_path_config.ao_upload',
'account_opening.file_path_config.upload',
```

同时目录 allowlist 也应包含旧目录和新目录，直到迁移完成。

#### 4. `getDocumentFileStream()` 有 `$app_id` 参数但未传给 DAO

当前方法签名：

```php
public function getDocumentFileStream(
  string $doc_id,
  string $doc_type,
  string $language,
  string $doc_suffix = '',
  string $type = '',
  string $app_id = ''
): bool|array
```

但内部调用：

```php
$lvDocumentData = $this->pvDao->getDocumentByTypeAndDocId($lvDocType, $doc_id);
```

建议继续传入 `$app_id`：

```php
$lvDocumentData = $this->pvDao->getDocumentByTypeAndDocId($lvDocType, $doc_id, $app_id);
```

原因：

- 之前已确认存在重复 `doc_id=disclaimer` 的历史风险。
- 若不带 app_id，stream 仍可能选到旧 document row。
- 这和本次开户协议书迁移虽然不是同一个 bug，但都影响 document stream 的正确性。

### 建议结论

这版代码已经解决“`account_opening.file_path_config.upload` 被 upload_type allowlist 直接拒绝”的问题。

但上线前仍建议至少修复：

1. `get('account_opening')` 改成 `get('upload')`。
2. 空目录配置不要 `return TRUE`，应只校验非空目录；没有目录时返回 `FALSE`。
3. 按上线顺序决定是否临时兼容 `account_opening.file_path_config.ao_upload`。
4. `getDocumentByTypeAndDocId()` 调用补传 `$app_id`。
