# OpenAPI 契约兼容性审计 API

用 Python + FastAPI 实现的 OpenAPI 3.x 契约服务，提供四个接口：

- **`POST /audit`**：提交**基线版**与**候选版**两份 OpenAPI 3.x JSON 文档，服务会解析本地 `$ref`（循环安全），并按以下策略判定候选版是否向后兼容：

  - **请求侧约束不得收紧**：删除操作、新增必填请求参数 / 请求体 / 必填字段、类型变更、枚举收窄等均为破坏性变更。
  - **响应侧约束不得放宽**：删除响应状态码 / 媒体类型 / 字段、字段不再必填、类型变更、枚举放宽等均为破坏性变更。

  报告按 **JSON Pointer 稳定排序**，每条结果包含 `pointer`、`category`、`severity`、`reason`；
  无法解析的引用会以 HTTP 422 返回明确错误。

- **`POST /slice`**：提交**一份** OpenAPI 3.x 文档与一组不重复的 `METHOD /path` 选择器，
  生成只包含所选操作的**最小子契约**，并返回键序稳定的子契约及其 **SHA-256** 摘要。

- **`POST /compose`**：提交 **2～20 份**各自带 `service_id` 与 `path_prefix` 的 OpenAPI 3.x 文档
  以及合成后的 `info`，生成一份**网关契约**及**来源映射**（provenance）：路径按规范化前缀挂载合并，
  组件名 / security 键 / `operationId` / tag 均以 `service_id` 为前缀命名空间化，
  指向 `components` 的本地 `$ref` 同步改写，根级 `security` 物化到未声明该字段的操作上。

- **`POST /examples/validate`**：提交**一份** OpenAPI 3.x 文档，按各媒体类型的 Schema 校验
  所有 `requestBody` 与响应 **JSON 媒体类型**（`application/json` 及 `*+json`）上的
  `example` 与 `examples.<name>.value`（含经本地 `$ref` 引用的 Example Object）。
  请求示例出现 `readOnly` 字段、响应示例出现 `writeOnly` 字段即报错，`required` 按方向生效，
  区分 3.0 `nullable` 与 3.1 类型联合；纯 Schema 违规返回 `200` + `valid=false`，
  `externalValue` / 外部或悬空引用返回 `422`。

## 一条命令启动

需要安装 Docker（含 Compose 插件）。在项目根目录执行：

```bash
docker compose up --build
```

启动后访问地址与端口：

| 用途 | 地址 |
| --- | --- |
| 健康检查 | http://localhost:8080/health |
| 审计接口 | `POST http://localhost:8080/audit` |
| 子契约切片接口 | `POST http://localhost:8080/slice` |
| 网关契约合成接口 | `POST http://localhost:8080/compose` |
| 示例校验接口 | `POST http://localhost:8080/examples/validate` |
| 交互式 API 文档（Swagger UI） | http://localhost:8080/docs |
| OpenAPI 描述文档 | http://localhost:8080/openapi.json |

> 容器内监听 `8080`，由 Compose 映射到宿主机同名端口。如宿主机 8080 被占用，
> 修改 `docker-compose.yml` 中 `"8080:8080"` 的左侧端口即可，例如 `"9090:8080"`。

后台运行与停止：

```bash
docker compose up --build -d   # 后台启动
docker compose down            # 停止并移除容器
```

## 调用示例

```bash
curl -s -X POST http://localhost:8080/audit \
  -H 'Content-Type: application/json' \
  -d '{"baseline": <基线版 OpenAPI JSON>, "candidate": <候选版 OpenAPI JSON>}'
```

请求体：

```json
{
  "baseline": { "openapi": "3.0.3", "paths": { "...": {} } },
  "candidate": { "openapi": "3.0.3", "paths": { "...": {} } }
}
```

成功响应（`200`）：

```json
{
  "compatible": false,
  "breaking_count": 2,
  "info_count": 1,
  "findings": [
    {
      "pointer": "/components/schemas/NewPet",
      "category": "required-added",
      "severity": "breaking",
      "reason": "property 'tag' became required in the request"
    },
    {
      "pointer": "/paths/~1pets/get/parameters",
      "category": "parameter-required-added",
      "severity": "breaking",
      "reason": "query parameter 'limit' became required; previously requests could omit it"
    },
    {
      "pointer": "/paths/~1pets",
      "category": "operation-added",
      "severity": "info",
      "reason": "new operation PUT /pets (compatible)"
    }
  ]
}
```

- `severity = "breaking"`：违反兼容性策略（请求收紧 / 响应放宽）。
- `severity = "info"`：安全方向的变更（新增操作、新增可选参数、请求枚举放宽等），仅作提示。
- `findings` 按 `(pointer, category, reason)` 升序稳定排序。

错误响应：

- `400 invalid_openapi_document`：缺少 `openapi: 3.x` 版本字段或 `paths` 对象。
- `422 unresolvable_reference`：存在无法解析的 `$ref`，错误体逐条给出文档归属、引用串与原因：

```json
{
  "error": "unresolvable_reference",
  "message": "one or more $ref entries could not be resolved; fix the references before the contract can be audited",
  "references": [
    {
      "document": "baseline",
      "ref": "#/components/schemas/DoesNotExist",
      "error": "unresolvable reference '#/components/schemas/DoesNotExist': object has no property 'DoesNotExist'"
    }
  ]
}
```

## `POST /slice`：抽取最小操作子契约

给定一份 OpenAPI 3.x 文档和一组不重复的 `METHOD /path` 选择器，服务生成只描述这些操作的最小子契约：

- 根节点只保留 **`openapi`** 与 **`info`**；其余根字段（`servers`、`tags`、`webhooks` 等）不进入子契约。
- `paths` 下只保留所选路径；**同一路径只保留被选中的方法与 path-level 字段**
  （共享的 `parameters`、`servers`、`summary`、`description`、`x-` 扩展等），
  其他 HTTP 方法一律剔除，不会混入。OpenAPI 3.1 的 path-level `$ref`（可带同级字段）会被解析并内联。
- 操作未声明 `security` 时**继承根级 `security`**（继承结果物化到操作上；显式 `security: []` 表示不鉴权，保持不变），
  并且 `components/securitySchemes` 只保留实际被引用到的方案；一个都没用到时整个 `components` 可以不出现。
- 从所有保留节点递归计算**本地 `$ref` 闭包**（含 `parameters` / `requestBody` / `responses` / schema 等任意位置，
  含 OpenAPI 3.1 `$ref` 的同级字段），并按**原 JSON Pointer** 在子契约中重建依赖，
  因此所有保留的引用仍然可解析。
- **循环引用安全**：循环目标只复制一次，不无限展开（包括 `Pet.parent -> Pet` 形式与纯 `A -> B -> A` 引用链）。
- **只有从保留节点可达的引用会被校验**：可达的悬空指针或非本地引用（`http(s)://`、文件路径等）返回 `422`；
  未选操作 / 未使用组件中的坏引用与本次切片无关，不会阻塞。
- 输出子契约的**对象键序稳定**（统一按键名排序的紧凑 JSON），并给出其 **SHA-256**；
  输入对象的键顺序与选择器顺序都不会影响子契约内容或摘要，同一选择器集合始终得到同一摘要。

### 请求

```bash
curl -s -X POST http://localhost:8080/slice \
  -H 'Content-Type: application/json' \
  -d '{
    "document": { "openapi": "3.0.3", "info": {"title": "Pet API", "version": "1.0"}, "paths": {"...": {}} },
    "selectors": ["GET /pets", "POST /pets/{petId}"]
  }'
```

- `document`：一份 OpenAPI 3.x JSON 文档（对象）。
- `selectors`：**非空**字符串数组，每项形如 `METHOD /path`；方法大小写不敏感（统一按小写匹配/输出），
  路径必须以 `/` 开头并与文档中的路径键逐字一致（路径参数写作 `GET /pets/{petId}`）。

### 成功响应（`200`）

```json
{
  "contract": {
    "openapi": "3.0.3",
    "info": {"title": "Pet API", "version": "1.0"},
    "paths": {
      "/pets": {
        "get": {
          "responses": {"200": {"content": {"application/json": {"schema": {
            "$ref": "#/components/schemas/Pets"}}}, "description": "ok"}},
          "security": [{"oauth": ["pets:read"]}]
        }
      }
    },
    "components": {
      "schemas": {
        "Pet": {"properties": {"id": {"$ref": "#/components/schemas/Id"},
                 "parent": {"$ref": "#/components/schemas/Pet"}}, "type": "object"},
        "Id": {"type": "integer"},
        "Pets": {"items": {"$ref": "#/components/schemas/Pet"}, "type": "array"}
      },
      "securitySchemes": {"oauth": {"flows": {}, "type": "oauth2"}}
    }
  },
  "sha256": "9f8c9a…（64 位十六进制）"
}
```

- `sha256` 是 `contract` 的规范化 JSON 编码（键名升序、无多余空白、UTF-8）的 SHA-256 十六进制摘要。
- 响应中的 `contract` 本身也按稳定键序返回，可直接重新序列化后自行复算摘要进行校验。

### 错误响应

- `400 invalid_openapi_document`：`document` 缺少 `openapi: 3.x` 或 `paths` 对象。
- `400 invalid_selector`：选择器问题，`messages` 逐条给出原因（顺序稳定，不随入参顺序变化）：
  - 选择器不是非空数组，或单项不满足 `METHOD /path` 形态（如 `"GET/pets"`、`"garbage"`）；
  - **重复选择器**（`GET /pets` 与 `get /pets` 视为同一个）；
  - **未知操作**：路径未声明，或该路径上不存在该方法（path-level `$ref` 目标提供的方法同样认可）。
- `422 unresolvable_reference`：所选操作（或其闭包依赖）中存在无法解析的引用，`references` 逐条给出引用串与原因：

```json
{
  "error": "unresolvable_reference",
  "message": "a $ref reachable from the selected operations could not be resolved; fix the reference or remove the operation that uses it",
  "references": [
    {
      "ref": "#/components/schemas/Missing",
      "error": "unresolvable reference '#/components/schemas/Missing': object has no property 'Missing'"
    }
  ]
}
```

> 400 与 422 的边界：选择器重复 / 指向不存在的方法属于请求错误（400）；
> 选择的方法确实存在、但其保留节点引用了悬空或非本地目标属于契约引用错误（422）。
> 未被选中的操作和组件即使含坏引用也不会被访问，因而不影响切片结果。

## `POST /compose`：合成多服务网关契约

将 **2～20 份** OpenAPI 3.x 源文档各自挂载到指定的路径前缀下，合成一份网关契约，
并返回覆盖每个输出路径、操作与组件的**来源映射**（provenance）。

### 合成规则

- **路径前缀规范化**：自动补前导 `/`、去除末尾 `/`；`""` 与 `"/"` 均表示挂载到网关根路径。
  前缀中不允许出现空白符、`?`、`#`。规范化后的前缀拼接到该源的每条路径前。
- **路径合并与冲突**：不同源贡献到同一合成路径的不同方法会合并进同一个 path item
  （path-level `parameters` 取并集去重，其余 path-level 字段按请求顺序**先写优先**）；
  **同一合成路径 + 同一方法**（方法名大小写不敏感）来自两个源时返回 **409**，并列出冲突双方来源。
  冲突判定会**循环安全地逐层解析 OpenAPI 3.1 path-level `$ref` 链**（目标与同级字段合并、同级优先），
  因此经 `components/pathItems` 引用提供的方法与直接声明的方法同等参与判定；
  409 的 `conflicts.sources` 分别指向各源文档中该操作的 JSON Pointer
  （引用提供的方法指向链终点组件内的操作位置）。
  多个源在同一合成路径上都使用 path-level `$ref` 时，合成 path item **保留先到的引用**，
  其余引用链提供的有效方法（同样按引用目标与同级字段的覆盖关系计算）会作为**同级操作内联**
  进合成 path item（OpenAPI 3.1 中 `$ref` 同级字段优先于引用目标），
  因此所有非冲突方法都能从合成路径解析，且内联操作已完成 security 物化与命名空间前缀化。
- **security 物化**：各源的根级 `security` 会物化到该源每个未声明 `security` 的操作上
  （显式 `security: []` 保持不变）；合成契约不再携带根级 `security`。
- **命名空间前缀**：组件名（`components` 下各 section 的键）、security requirement 的键、
  `operationId`、tag（操作级 `tags` 与根级 `tags` 声明）统一加上 `<service_id>_` 前缀，
  因此跨服务同名组件不会冲突。`service_id` 只能包含字母、数字、`.`、`_`、`-`（须以字母或数字开头），
  以保证前缀化后的组件键仍是合法的 OpenAPI 组件名。
- **`$ref` 改写**：所有指向 `components` 的本地引用（`#/components/<section>/<name>[/...]`，
  支持指向组件内部的深层指针）同步改写为前缀化后的名字；OpenAPI 3.1 的 path-level `$ref`
  （指向 `components/pathItems`）同样改写并保持引用形态，目标组件内的操作照常物化 security、加前缀。
- **引用限制（422）**：外部引用（`http(s)://`、文件路径等）、指向 `components` 以外位置的本地引用
  （如 `#/paths/...`、`#`、`#/info`）、悬空引用均返回 **422**，错误体逐条给出
  `service_id`、源文档中的 **JSON Pointer**、引用串与原因；path-level `$ref` 的**循环引用链**
  （含超过 100 层的异常链）与无法解析为对象的 path item 引用同样按此格式拒绝。
  只校验进入合成契约的内容
  （`paths` / `components` / `tags`）：被丢弃的根字段（`servers`、`webhooks`、`externalDocs`、
  `x-` 扩展等）中的坏引用不会阻塞合成。
- **其余根字段**：`openapi` 取各源最高版本；`info` 使用请求中给定的合成版本；
  根级 `tags` 合并（按前缀化后的名字去重，先写优先）。
- **错误优先级**：请求错误（400）→ 引用错误（422）→ 操作冲突（409）。

### 请求

```bash
curl -s -X POST http://localhost:8080/compose \
  -H 'Content-Type: application/json' \
  -d '{
    "info": {"title": "Gateway", "version": "1.0"},
    "services": [
      {"service_id": "pets", "path_prefix": "/pets", "document": { "openapi": "3.0.3", "...": "..." }},
      {"service_id": "store", "path_prefix": "/store", "document": { "openapi": "3.1.0", "...": "..." }}
    ]
  }'
```

- `info`：合成契约的 info 对象（原样使用）。
- `services`：**2～20** 个源服务，每项含 `service_id`（唯一）、`path_prefix`、`document`（OpenAPI 3.x 文档）。

### 成功响应（`200`）

```json
{
  "contract": {
    "openapi": "3.1.0",
    "info": {"title": "Gateway", "version": "1.0"},
    "tags": [{"name": "pets_animals", "description": "Animal operations"}],
    "paths": {
      "/pets/animals": {
        "get": {
          "operationId": "pets_listAnimals",
          "tags": ["pets_animals"],
          "security": [{"pets_oauth": ["animals:read"]}],
          "responses": {"200": {"content": {"application/json": {"schema": {
            "$ref": "#/components/schemas/pets_Animal"}}}, "description": "ok"}}
        }
      },
      "/store/items": {"get": {"operationId": "store_listItems", "responses": {"...": "..."}}}
    },
    "components": {
      "schemas": {"pets_Animal": {"...": "..."}, "store_Item": {"...": "..."}},
      "securitySchemes": {"pets_oauth": {"flows": {}, "type": "oauth2"}}
    }
  },
  "provenance": {
    "paths": {
      "/pets/animals": [
        {"service_id": "pets", "source_path": "/animals", "source_pointer": "/paths/~1animals"}
      ]
    },
    "operations": {
      "/pets/animals": {
        "get": {"service_id": "pets", "source_path": "/animals",
                "source_pointer": "/paths/~1animals/get"}
      }
    },
    "components": {
      "schemas": {
        "pets_Animal": {"service_id": "pets", "source_pointer": "/components/schemas/Animal"}
      }
    }
  }
}
```

- `provenance.paths`：每个合成路径 → 贡献它的源服务列表（含源路径与源 JSON Pointer）。
- `provenance.operations`：每个合成路径的每个方法 → 源操作位置。
- `provenance.components`：每个组件 section 的每个前缀化名字 → 源组件位置。

### 错误响应

- `400 invalid_compose_request`：服务数量不在 2～20、`service_id` 缺失 / 重复 / 含非法字符、
  `path_prefix` 含空白或 `?`/`#`、文档不是 OpenAPI 3.x、路径键未以 `/` 开头等；`messages` 逐条给出原因。
- `409 operation_conflict`：两个源声明了同一合成路径的同一方法；`conflicts` 逐条给出
  `path`、`method` 与冲突**双方**的来源（`service_id` + 源 JSON Pointer）：

```json
{
  "error": "operation_conflict",
  "message": "multiple services declare the same composed path and HTTP method; ...",
  "conflicts": [
    {
      "path": "/pets/animals",
      "method": "get",
      "sources": [
        {"service_id": "alpha", "source_path": "/animals", "source_pointer": "/paths/~1animals/get"},
        {"service_id": "beta", "source_path": "/animals", "source_pointer": "/paths/~1animals/get"}
      ]
    }
  ]
}
```

- `422 unsupported_reference`：保留内容中存在外部引用、指向 `components` 以外的引用或悬空引用；
  `references` 逐条给出 `service_id`、源文档中的 JSON Pointer、引用串与原因：

```json
{
  "error": "unsupported_reference",
  "message": "one or more retained $ref entries are external, unresolvable, or point outside components; ...",
  "references": [
    {
      "service_id": "pets",
      "pointer": "/paths/~1animals/get/responses/200/content/application~1json/schema",
      "ref": "https://example.com/schemas.json#/Animal",
      "error": "unsupported non-local reference 'https://example.com/schemas.json#/Animal': ..."
    }
  ]
}
```

> 与 `/audit`、`/slice` 一致：保留内容中任何位置的 `"$ref"` 字符串（包括示例值内部）都按引用处理。
> 混合 3.0 / 3.1 源时合成契约取最高版本，但 schema 方言差异（如 `nullable`）由调用方自行保证兼容。

## `POST /examples/validate`：校验请求 / 响应示例

给定**一份** OpenAPI 3.x 文档，服务遍历每个操作的 `requestBody` 与各响应，
对其中 **JSON 媒体类型**（精确匹配 `application/json`，或任意 `*+json` 结构媒体类型；
`text/plain`、`application/octet-stream` 等不检查）携带的示例逐一按该媒体类型的 `schema` 校验：

- Media Type Object 上的内联 `example`；
- `examples` 映射中的每个 Example Object，包括经本地 `$ref`
  （`#/components/examples/...`、`#/components/requestBodies/...`、
  `#/components/responses/...`、3.1 `components/pathItems/...`）解析到的对象；
  校验所用的 **Schema `$ref` 同样解析**（含递归 Schema，循环安全）。

### 方向性语义（OpenAPI 规范）

- **请求**示例中出现 Schema 标记为 `readOnly: true` 的属性 → 报错（`keyword: "readOnly"`）；
  **响应**示例中出现 `writeOnly: true` 的属性 → 报错（`keyword: "writeOnly"`）。
- `required` **只在对应方向生效**：`readOnly` 属性即使列入 `required`，也只要求响应必带
  （请求示例缺失不报错）；`writeOnly` 属性相反，只要求请求必带。
- **类型可空性按版本区分**：OpenAPI 3.0 用 `nullable: true` 放行 `null`；
  OpenAPI 3.1（JSON Schema 2020-12 方言）用类型联合 `"type": ["string", "null"]`，
  此时 `nullable` 关键字被忽略。`integer` 是 `number` 的子集；无小数部分的数字也满足 `integer`。

### 支持的 Schema 关键字

`type` / `enum` / `const` / `required` / `properties` / `additionalProperties` /
`minProperties` / `maxProperties` / `items`（含 3.1 位置元组形态）/ `minItems` / `maxItems` /
`uniqueItems` / `minLength` / `maxLength` / `pattern` / `minimum` / `maximum` /
`exclusiveMinimum` / `exclusiveMaximum`（3.0 布尔修饰符与 3.1 数值边界两种写法均支持）/
`multipleOf` / `allOf` / `anyOf` / `oneOf`，以及布尔 Schema（3.1）。`$ref` 可出现在任意位置。

### 请求

```bash
curl -s -X POST http://localhost:8080/examples/validate \
  -H 'Content-Type: application/json' \
  -d '{"document": { "openapi": "3.0.3", "info": {"title": "Pet API", "version": "1.0"}, "paths": {"...": {}} }}'
```

### 成功响应（`200`，即使校验失败也是 200）

```json
{
  "valid": false,
  "error_count": 2,
  "errors": [
    {
      "side": "request",
      "example_pointer": "/paths/~1pets/post/requestBody/content/application~1json/example",
      "instance_pointer": "/created",
      "schema_pointer": "/components/schemas/Pet/properties/created",
      "keyword": "readOnly",
      "reason": "readOnly property 'created' must not appear in a request example"
    },
    {
      "side": "response",
      "example_pointer": "/paths/~1pets/post/responses/200/content/application~1json/examples/byId/value",
      "instance_pointer": "/id",
      "schema_pointer": "/components/schemas/Pet/properties/id",
      "keyword": "type",
      "reason": "expected type integer, got string"
    }
  ]
}
```

字段说明：

- `side`：示例方向，`request`（来自 `requestBody`）或 `response`（来自响应）。
- `example_pointer`：示例值在文档中的 JSON Pointer。经 `$ref` 引用的 Example Object
  会**跟随到组件定义位置**（如 `/components/examples/byId/value`），与 `/audit` 的 Pointer 约定一致；
  同一组件示例被多处复用时，相同违规只报告一次。
- `instance_pointer`：违规值在示例 JSON 内部的 JSON Pointer（根示例为 `""`，深层如 `/tags/0/name`）。
- `schema_pointer`：被违反的 Schema 位置（本地 `$ref` 跟随到组件定义）。
- `keyword` / `reason`：违反的 Schema 关键字与人类可读原因。
- `errors` 按 **`(example_pointer, instance_pointer, schema_pointer, keyword, reason)` 升序稳定排序**，
  与输入键序无关。

### 错误响应

- `400 invalid_openapi_document`：`document` 缺少 `openapi: 3.x` 或 `paths` 对象。
- `422 unsupported_reference`：示例使用了 `externalValue`（服务不会发起外部网络请求），
  或示例 / Schema 中出现外部 `$ref`（`http(s)://`、文件路径等）。
- `422 unresolvable_reference`：校验过程中可达的本地 `$ref` 悬空（包括示例组件间的纯 `$ref` 循环链）。
- `422 invalid_example`：示例 `$ref` 目标不是 Example Object。

422 响应体的 `references` 逐条给出**来源 JSON Pointer**（引用所在位置）、引用串与原因：

```json
{
  "error": "unresolvable_reference",
  "message": "one or more $ref entries reached while validating examples could not be resolved; fix the references first",
  "references": [
    {
      "pointer": "/paths/~1pets/get/responses/200/content/application~1json/schema",
      "ref": "#/components/schemas/Missing",
      "error": "unresolvable reference '#/components/schemas/Missing': object has no property 'Missing'"
    }
  ]
}
```

> 只有从被检查示例可达的引用才会触发 422；未被任何示例使用的组件里即使有坏引用也不影响结果。
> 媒体类型未声明 `schema` 时，示例不受任何约束（但 `externalValue` / 坏引用仍按上述规则返回 422）。
> Example Object 既无 `value` 也无 `externalValue` 时无可校验内容，跳过不报。

## 检查的变更类别

| category | 说明 | 判定为 breaking 的方向 |
| --- | --- | --- |
| `operation-removed` / `operation-added` | 路径上 HTTP 操作被删除 / 新增 | 删除操作 |
| `path-removed` / `path-added` | 整条路径被删除 / 新增 | 删除路径 |
| `parameter-added` / `parameter-removed` | 新增 / 删除参数（含路径级参数） | 新增**必填**参数 |
| `parameter-required-added` / `parameter-required-removed` | 参数必填性变化 | 参数由可选变必填 |
| `request-body-added` / `request-body-required-added` | 新增请求体或请求体变为必填 | 必填请求体被引入 |
| `type-changed` | 类型 / 可空性变化（参数、请求体、响应字段、数组 items），按值集合包含关系判定 | 请求侧收紧（如 `number`→`integer`、去除可空、新增类型约束）；响应侧放宽（如 `integer`→`number`、3.0 `nullable` / 3.1 `"null"` 联合新增可空） |
| `enum-changed` | 枚举值增删；缺省 `enum` 视为**无约束全集**而非空集 | 请求侧收紧（枚举收窄、新引入 enum）；响应侧放宽（枚举放宽、移除 enum 约束） |
| `required-added` / `required-removed` | 对象属性必填性变化 | 请求新增必填字段、响应取消必填保证 |
| `property-added` / `property-removed` | 对象属性增删 | 请求新增必填属性、响应删除属性 |
| `response-removed` / `response-added` | 响应状态码（含 `2XX` 范围码）增删 | 删除响应码 |
| `response-body-removed` | 响应体被移除 | 始终 breaking |
| `media-type-removed` / `media-type-added` | 请求/响应的媒体类型增删 | 删除调用方依赖的媒体类型 |
| `schema-variants-changed` | `oneOf` / `anyOf` 分支数量变化 | 始终 breaking |

### 类型与枚举的集合判定细则

schema 约束按**可接受 / 可产出的值集合**比较，而非关键字字面相等：

- **缺省即无约束**：未声明 `type` 视为接受所有基础类型，未声明 `enum` 视为接受该类型下所有值（均为全集，而非空集）。
  因此请求侧新增 `type` / `enum` 是收紧（breaking），响应侧移除 `type` / `enum` 约束是放宽（breaking）；反方向为 info。
- **integer 与 number 的包含关系**：`integer ⊂ number`。请求侧 `integer → number`（放宽）为 info，`number → integer`（收紧）为 breaking；
  响应侧结论相反（`integer → number` 可能产出小数，为 breaking）。
- **OpenAPI 3.0 `nullable: true`** 等价于在值集合中加入 `null`；与 OpenAPI 3.1 的类型联合 `"type": ["string", "null"]` 互相等价，
  两种写法之间迁移不报告。3.1 文档中的 `nullable` 关键字会被忽略（它不是 JSON Schema 2020-12 的关键字）。
- 当 `enum` 存在且未显式列出 `null` 时，`null` 不在允许集合内（此时 `nullable` / 联合中的 `"null"` 不生效）。

## JSON Pointer 约定

- **删除类**变更指向**基线文档**中的位置；**新增 / 修改类**变更指向**候选文档**中的位置。
- 节点为 `$ref` 时，Pointer 会跟随到最终组件定义，例如
  `#/components/schemas/Pet/properties/status`，这是修复契约问题的稳定位置；同一组件被多处引用时
  问题只需在组件定义处修复一次。
- Pointer 遵循 RFC 6901 转义（`~0` → `~`，`~1` → `/`），例如路径 `/pets` 显示为 `/paths/~1pets`。

## `$ref` 解析与循环引用保护

- 仅支持文档内本地引用（`#/...`，含根引用 `#`）；远程 URL、文件路径等非本地引用一律报为
  `unresolvable_reference`（服务不会发起任何外部网络请求）。
- 引用链记录已访问指针：
  - 正常的**递归 schema**（`Node.children -> #/components/schemas/Node`）会被识别并在成环处停止遍历，
    不会无限展开；
  - 纯 `$ref` 死循环（`A -> B -> A`）抛出明确的 `circular reference detected` 错误；
  - 超过 100 层的异常引用链有硬性上限保护；
- 结构比较阶段以“组件指针 / 节点身份 + 比较方向”做记忆化去重，并带有 300 层内联嵌套深度上限，
  双重保证循环引用不会失控。

## 本地开发（不使用 Docker）

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8080
```

运行测试：

```bash
pytest -q
```

测试覆盖：引用解析（缺失指针、数组越界、远程引用、转义 token、自循环）、操作/参数增删、
类型/枚举/required 双向规则、integer/number 包含、缺省约束、3.0 `nullable` 与 3.1 类型联合、
响应码与媒体类型删除、递归 schema 不爆炸、报告排序；以及 `/slice` 的子契约裁剪、
`$ref` 传递闭包与循环复制一次、未选分支坏引用不阻塞、`security` 继承与 `securitySchemes` 过滤、
3.1 path-level `$ref` 内联、400/422 边界、SHA-256 与键序 / 选择器顺序无关性；
以及 `/compose` 的前缀规范化与根挂载、路径合并与 409 冲突双方来源、security 物化与键前缀、
组件 / `operationId` / tag 命名空间化、`$ref` 改写与 422（外部 / 非 components / 悬空，含源 JSON Pointer）、
path-level `$ref` 链（含多级与中间层同级方法）的冲突检测与来源指针、
同一合成路径上多条 `$ref` 链的合并（非冲突方法内联后均可从输出路径解析、有效方法按同级覆盖取舍）、
循环 / 超深引用链 422、
来源映射完整性、2～20 数量边界、重复 `service_id` 400、被丢弃根字段中的坏引用不阻塞；
以及 `/examples/validate` 的内联 `example` 与 `examples.<name>.value`（含本地 `$ref` 示例组件）校验、
`readOnly` / `writeOnly` 方向报错、`required` 仅对应方向生效、3.0 `nullable` 与 3.1 类型联合、
integer/number 与布尔值区分、enum/const/allOf/anyOf/oneOf/数值·字符串·数组·对象约束关键字、
递归 Schema 不爆炸、按示例与实例 Pointer 稳定排序、同一示例组件复用去重、
`externalValue` / 外部引用 / 悬空引用 / 非对象示例引用 422（含来源 Pointer）、
不可达坏引用不阻塞、非 JSON 媒体类型跳过、400 文档形态错误等共 178 个用例。

## 项目结构

```
.
├── app/
│   ├── __init__.py
│   ├── main.py          # FastAPI 路由、请求/响应模型、输入校验（/audit、/slice、/compose 与 /examples/validate）
│   ├── audit.py         # 契约比较引擎（操作/参数/请求体/响应/schema）
│   ├── slicer.py        # 最小子契约切片（选择器校验、security 继承、$ref 闭包重建、SHA-256）
│   ├── composer.py      # 网关契约合成（前缀规范化、命名空间化、$ref 改写、冲突检测、来源映射）
│   ├── examples.py      # 媒体类型示例校验（方向化 readOnly/writeOnly/required、3.0/3.1 可空性、JSON Schema 关键字）
│   └── refresolver.py   # 本地 $ref 解析（RFC 6901 指针 + 循环检测）
├── tests/
│   ├── test_audit_api.py
│   ├── test_slice_api.py
│   ├── test_compose_api.py
│   └── test_examples_api.py
├── Dockerfile           # 容器封装全部依赖，非 root 用户运行，含 HEALTHCHECK
├── docker-compose.yml   # 一条命令启动，映射 8080 端口
├── requirements.txt
├── requirements-dev.txt
└── README.md
```
