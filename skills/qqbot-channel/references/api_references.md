# QQ 棰戦亾 API 瀹屾暣鍙傝€?
鏈枃妗ｅ寘鍚?QQ 寮€鏀惧钩鍙伴閬撶浉鍏虫墍鏈夋帴鍙ｇ殑璇︾粏鍙傛暟璇存槑銆佽繑鍥炲€肩粨鏋勫拰鏋氫妇鍊煎畾涔夈€?
閫氳繃 `qqbot_platform_api` 宸ュ叿浠ｇ悊璇锋眰锛屽伐鍏疯嚜鍔ㄥ鐞嗛壌鏉冦€?
---

## 馃搶 閫氱敤璇存槑

### 鍩虹 URL

`https://api.sgroup.qq.com`

### 閴存潈锛堣嚜鍔ㄥ鐞嗭級

宸ュ叿鑷姩濉厖浠ヤ笅璇锋眰澶达紝鏃犻渶鎵嬪姩璁剧疆锛?
```
Authorization: QQBot {access_token}
Content-Type: application/json
```

### 閿欒杩斿洖鏍煎紡

```json
{
  "message": "閿欒鎻忚堪",
  "code": 閿欒鐮?}
```

---

## 馃摝 杩斿洖鍊肩被鍨嬪畾涔?
### Guild锛堥閬擄級

```typescript
interface Guild {
  id: string;           // 棰戦亾 ID
  name: string;         // 棰戦亾鍚嶇О
  icon: string;         // 棰戦亾澶村儚 URL
  owner_id: string;     // 棰戦亾鎷ユ湁鑰?ID
  owner: boolean;       // 鏈哄櫒浜烘槸鍚︿负棰戦亾鎷ユ湁鑰?  joined_at: string;    // 鏈哄櫒浜哄姞鍏ユ椂闂达紙ISO 8601锛?  member_count: number; // 棰戦亾鎴愬憳鏁?  max_members: number;  // 棰戦亾鏈€澶ф垚鍛樻暟
  description: string;  // 棰戦亾鎻忚堪
}
```

### Channel锛堝瓙棰戦亾锛?
```typescript
interface Channel {
  id: string;                  // 瀛愰閬?ID
  guild_id: string;            // 鎵€灞為閬?ID
  name: string;                // 瀛愰閬撳悕绉?  type: number;                // 瀛愰閬撶被鍨嬶紙瑙佹灇涓撅級
  position: number;            // 鎺掑簭浣嶇疆
  parent_id: string;           // 鎵€灞炲垎缁?ID
  owner_id: string;            // 鍒涘缓鑰?ID
  sub_type: number;            // 瀛愮被鍨嬶紙瑙佹灇涓撅級
  private_type?: number;       // 绉佸瘑绫诲瀷锛堣鏋氫妇锛?  speak_permission?: number;   // 鍙戣█鏉冮檺锛堣鏋氫妇锛?  application_id?: string;     // 搴旂敤瀛愰閬?AppID
}
```

### User锛堢敤鎴凤級

```typescript
interface User {
  id: string;                    // 鐢ㄦ埛 ID
  username: string;              // 鐢ㄦ埛鍚?  avatar: string;                // 澶村儚 URL
  bot: boolean;                  // 鏄惁涓烘満鍣ㄤ汉
  union_openid?: string;         // 鐗规畩鍏宠仈搴旂敤鐨?openid
  union_user_account?: string;   // 鐗规畩鍏宠仈搴旂敤鐨勭敤鎴蜂俊鎭?}
```

### Member锛堟垚鍛橈級

```typescript
interface Member {
  user: User;          // 鐢ㄦ埛鍩烘湰淇℃伅
  nick: string;        // 鍦ㄩ閬撲腑鐨勬樀绉?  roles: string[];     // 韬唤缁?ID 鍒楄〃
  joined_at: string;   // 鍔犲叆棰戦亾鏃堕棿锛圛SO 8601锛?  deaf?: boolean;      // 鏄惁琚瑷€
  mute?: boolean;      // 鏄惁琚棴楹?  pending?: boolean;   // 鏄惁寰呭鏍?}
```

### APIPermission锛圓PI 鏉冮檺锛?
```typescript
interface APIPermission {
  path: string;        // 鎺ュ彛璺緞
  method: string;      // 璇锋眰鏂规硶
  desc: string;        // 鎺ュ彛鎻忚堪
  auth_status: number; // 鎺堟潈鐘舵€侊細0=鏈巿鏉? 1=宸叉巿鏉?}
```

### AnnouncesResult锛堝叕鍛婄粨鏋滐級

```typescript
interface AnnouncesResult {
  guild_id: string;
  channel_id: string;
  message_id: string;
  announces_type: number;
  recommend_channels: RecommendChannel[];
}

interface RecommendChannel {
  channel_id: string;  // 鎺ㄨ崘鐨勫瓙棰戦亾 ID
  introduce: string;   // 鎺ㄨ崘璇?}
```

### ThreadDetail锛堝笘瀛愯鎯咃級

```typescript
interface ThreadDetail {
  thread: {
    guild_id: string;
    channel_id: string;
    author_id: string;
    thread_info: {
      thread_id: string;
      title: string;
      content: string;
      date_time: string;
    };
  };
}
```

### ThreadListResult锛堝笘瀛愬垪琛級

```typescript
interface ThreadListResult {
  threads: Array<{
    guild_id: string;
    channel_id: string;
    author_id: string;
    thread_info: {
      thread_id: string;
      title: string;
      content: string;
      date_time: string;
    };
  }>;
  is_finish: number;  // 1=宸插埌搴? 0=杩樻湁鏇村
}
```

### Schedule锛堟棩绋嬶級

```typescript
interface Schedule {
  id?: string;
  name: string;
  start_timestamp: string;  // 姣绾ф椂闂存埑
  end_timestamp: string;
  jump_channel_id?: string;
  remind_type?: string;
  creator?: {
    user: { id: string; username: string; bot: boolean };
    nick: string;
    joined_at: string;
  };
}
```

---

## 馃搵 鏋氫妇鍊煎畾涔?
### 瀛愰閬撶被鍨嬶紙Channel type锛?
| 鍊?| 鍚嶇О | 璇存槑 |
|----|------|------|
| `0` | 鏂囧瓧瀛愰閬?| 鏅€氭枃瀛楄亰澶?|
| `2` | 璇煶瀛愰閬?| 璇煶鑱婂ぉ |
| `4` | 瀛愰閬撳垎缁?| 缁勭粐瀛愰閬撶殑鍒嗙粍锛坧osition 鈮?2锛?|
| `10005` | 鐩存挱瀛愰閬?| 鐩存挱鍔熻兘 |
| `10006` | 搴旂敤瀛愰閬?| 闇€ application_id |
| `10007` | 璁哄潧瀛愰閬?| 璁哄潧鍔熻兘 |

### 瀛愰閬撳瓙绫诲瀷锛圕hannel sub_type锛?
| 鍊?| 鍚嶇О |
|----|------|
| `0` | 闂茶亰 |
| `1` | 鍏憡 |
| `2` | 鏀荤暐 |
| `3` | 寮€榛?|

### 瀛愰閬撶瀵嗙被鍨嬶紙Channel private_type锛?
| 鍊?| 璇存槑 |
|----|------|
| `0` | 鍏紑瀛愰閬?|
| `1` | 绠＄悊鍛樺拰鎸囧畾鎴愬憳鍙 |
| `2` | 浠呯鐞嗗憳鍙 |

### 瀛愰閬撳彂瑷€鏉冮檺锛圕hannel speak_permission锛?
| 鍊?| 璇存槑 |
|----|------|
| `0` | 鏃犳晥锛堜粎鍒涘缓鍏憡瀛愰閬撴椂鏈夋晥锛屾鏃朵负鍙锛?|
| `1` | 鎵€鏈変汉鍙彂瑷€ |
| `2` | 浠呯鐞嗗憳鍜屾寚瀹氭垚鍛樺彲鍙戣█ |

### 鍏憡绫诲瀷锛坅nnounces_type锛?
| 鍊?| 璇存槑 |
|----|------|
| `0` | 鎴愬憳鍏憡 |
| `1` | 娆㈣繋鍏憡 |

### 甯栧瓙鏍煎紡锛坒ormat锛?
| 鍊?| 鏍煎紡 |
|----|------|
| `1` | 绾枃鏈?|
| `2` | HTML |
| `3` | Markdown锛?*榛樿**锛?|
| `4` | JSON锛圧ichText锛?|

### 鏃ョ▼鎻愰啋绫诲瀷锛坮emind_type锛?
| 鍊?| 璇存槑 |
|----|------|
| `"0"` | 涓嶆彁閱?|
| `"1"` | 寮€濮嬫椂鎻愰啋 |
| `"2"` | 寮€濮嬪墠 5 鍒嗛挓 |
| `"3"` | 寮€濮嬪墠 15 鍒嗛挓 |
| `"4"` | 寮€濮嬪墠 30 鍒嗛挓 |
| `"5"` | 寮€濮嬪墠 60 鍒嗛挓 |

### API 鏉冮檺鎺堟潈鐘舵€侊紙auth_status锛?
| 鍊?| 璇存槑 |
|----|------|
| `0` | 鏈巿鏉?|
| `1` | 宸叉巿鏉?|

---

## 馃摉 鍚勬帴鍙ｈ缁嗚鏄?
### GET /users/@me/guilds 鈥?鑾峰彇棰戦亾鍒楄〃

**鏌ヨ鍙傛暟**:

| 鍙傛暟 | 绫诲瀷 | 蹇呭～ | 璇存槑 |
|------|------|------|------|
| `before` | string | 鍚?| 璇绘 guild id 涔嬪墠鐨勬暟鎹?|
| `after` | string | 鍚?| 璇绘 guild id 涔嬪悗鐨勬暟鎹紙涓?before 鍚屾椂璁剧疆鏃舵棤鏁堬級 |
| `limit` | string | 鍚?| 姣忔鎷夊彇鏉℃暟锛岄粯璁?100锛屾渶澶?100 |

**杩斿洖**: `Guild[]`

**璋冪敤绀轰緥**:

```json
{ "method": "GET", "path": "/users/@me/guilds", "query": { "limit": "100" } }
```

---

### GET /guilds/{guild_id}/api_permission 鈥?鑾峰彇棰戦亾 API 鏉冮檺

**杩斿洖**: `{ apis: APIPermission[] }`

**璋冪敤绀轰緥**:

```json
{ "method": "GET", "path": "/guilds/123456/api_permission" }
```

---

### GET /guilds/{guild_id}/channels 鈥?鑾峰彇瀛愰閬撳垪琛?
**杩斿洖**: `Channel[]`

**璋冪敤绀轰緥**:

```json
{ "method": "GET", "path": "/guilds/123456/channels" }
```

---

### GET /channels/{channel_id} 鈥?鑾峰彇瀛愰閬撹鎯?
**杩斿洖**: `Channel`

---

### POST /guilds/{guild_id}/channels 鈥?鍒涘缓瀛愰閬?
> 鈿狅笍 浠呯鍩熸満鍣ㄤ汉鍙敤锛岄渶绠＄悊棰戦亾鏉冮檺

**璇锋眰浣?*:

| 鍙傛暟 | 绫诲瀷 | 蹇呭～ | 璇存槑 |
|------|------|------|------|
| `name` | string | 鏄?| 瀛愰閬撳悕绉?|
| `type` | number | 鏄?| 瀛愰閬撶被鍨?|
| `position` | number | 鏄?| 鎺掑簭浣嶇疆锛坱ype=4 鏃?鈮?2锛?|
| `sub_type` | number | 鍚?| 瀛愮被鍨?|
| `parent_id` | string | 鍚?| 鎵€灞炲垎缁?ID |
| `private_type` | number | 鍚?| 绉佸瘑绫诲瀷 |
| `private_user_ids` | string[] | 鍚?| 绉佸瘑鎴愬憳鍒楄〃锛坧rivate_type=1 鏃舵湁鏁堬級 |
| `speak_permission` | number | 鍚?| 鍙戣█鏉冮檺 |
| `application_id` | string | 鍚?| 搴旂敤 AppID锛坱ype=10006 鏃堕渶瑕侊級 |

**杩斿洖**: `Channel`

---

### PATCH /channels/{channel_id} 鈥?淇敼瀛愰閬?
> 鈿狅笍 浠呯鍩熸満鍣ㄤ汉鍙敤

**璇锋眰浣?*锛堣嚦灏戜竴涓級:

| 鍙傛暟 | 绫诲瀷 | 璇存槑 |
|------|------|------|
| `name` | string | 鍚嶇О |
| `position` | number | 鎺掑簭浣嶇疆 |
| `parent_id` | string | 鍒嗙粍 ID |
| `private_type` | number | 绉佸瘑绫诲瀷 |
| `speak_permission` | number | 鍙戣█鏉冮檺 |

**杩斿洖**: `Channel`

---

### DELETE /channels/{channel_id} 鈥?鍒犻櫎瀛愰閬?
> 鈿狅笍 涓嶅彲閫嗭紒浠呯鍩熸満鍣ㄤ汉鍙敤

---

### GET /guilds/{guild_id}/members 鈥?鑾峰彇鎴愬憳鍒楄〃

> 浠呯鍩熸満鍣ㄤ汉鍙敤

**鏌ヨ鍙傛暟**:

| 鍙傛暟 | 绫诲瀷 | 璇存槑 |
|------|------|------|
| `after` | string | 涓婃鏈€鍚庝竴涓?user.id锛岄娆″～ `"0"` |
| `limit` | string | 鍒嗛〉澶у皬 1-400锛岄粯璁?1 |

**杩斿洖**: `Member[]`

> 缈婚〉锛氱敤鏈€鍚庝竴涓?`user.id` 浣滀负 `after`锛岀洿鍒拌繑鍥炵┖鏁扮粍銆傚彲鑳借繑鍥為噸澶嶆垚鍛橈紝闇€鎸?`user.id` 鍘婚噸銆?
---

### GET /guilds/{guild_id}/members/{user_id} 鈥?鑾峰彇鎴愬憳璇︽儏

**杩斿洖**: `Member`

---

### GET /guilds/{guild_id}/roles/{role_id}/members 鈥?鑾峰彇韬唤缁勬垚鍛樺垪琛?
> 浠呯鍩熸満鍣ㄤ汉鍙敤

**鏌ヨ鍙傛暟**:

| 鍙傛暟 | 绫诲瀷 | 璇存槑 |
|------|------|------|
| `start_index` | string | 鍒嗛〉鏍囪瘑锛岄娆″～ `"0"` |
| `limit` | string | 鍒嗛〉澶у皬 1-400锛岄粯璁?1 |

**杩斿洖**: `{ data: Member[], next: string }`

> 缈婚〉锛氱敤 `next` 浣滀负 `start_index`锛岀洿鍒?`data` 涓虹┖銆?
---

### GET /channels/{channel_id}/online_nums 鈥?鑾峰彇鍦ㄧ嚎鎴愬憳鏁?
**杩斿洖**: `{ online_nums: number }`

---

### POST /guilds/{guild_id}/announces 鈥?鍒涘缓棰戦亾鍏憡

**璇锋眰浣?*:

| 鍙傛暟 | 绫诲瀷 | 蹇呭～ | 璇存槑 |
|------|------|------|------|
| `message_id` | string | 鍚?| 娑堟伅 ID锛堟湁鍊兼椂鍒涘缓娑堟伅鍏憡锛屾鏃?channel_id 蹇呭～锛?|
| `channel_id` | string | 鍚?| 瀛愰閬?ID |
| `announces_type` | number | 鍚?| 0=鎴愬憳鍏憡锛?=娆㈣繋鍏憡 |
| `recommend_channels` | array | 鍚?| 鎺ㄨ崘瀛愰閬撳垪琛紙鏈€澶?3 鏉★紝message_id 涓虹┖鏃剁敓鏁堬級 |

> 涓ょ鍏憡绫诲瀷浼氫簰鐩搁《鏇?
**杩斿洖**: `AnnouncesResult`

---

### DELETE /guilds/{guild_id}/announces/{message_id} 鈥?鍒犻櫎鍏憡

> `message_id` 璁句负 `all` 鍒犻櫎鎵€鏈夊叕鍛?
---

### GET /channels/{channel_id}/threads 鈥?鑾峰彇甯栧瓙鍒楄〃

> 浠呯鍩熸満鍣ㄤ汉鍙敤锛宑hannel_id 椤讳负璁哄潧瀛愰閬擄紙type=10007锛?
**杩斿洖**: `ThreadListResult`

---

### GET /channels/{channel_id}/threads/{thread_id} 鈥?鑾峰彇甯栧瓙璇︽儏

> 浠呯鍩熸満鍣ㄤ汉鍙敤

**杩斿洖**: `ThreadDetail`

---

### PUT /channels/{channel_id}/threads 鈥?鍙戣〃甯栧瓙

> 浠呯鍩熸満鍣ㄤ汉鍙敤

**璇锋眰浣?*:

| 鍙傛暟 | 绫诲瀷 | 蹇呭～ | 璇存槑 |
|------|------|------|------|
| `title` | string | 鏄?| 甯栧瓙鏍囬 |
| `content` | string | 鏄?| 甯栧瓙鍐呭 |
| `format` | number | 鍚?| 1=鏂囨湰, 2=HTML, 3=Markdown锛堥粯璁わ級, 4=JSON |

**杩斿洖**: `{ task_id: string, create_time: string }`

---

### DELETE /channels/{channel_id}/threads/{thread_id} 鈥?鍒犻櫎甯栧瓙

> 鈿狅笍 涓嶅彲閫嗭紒浠呯鍩熸満鍣ㄤ汉鍙敤

---

### POST /channels/{channel_id}/threads/{thread_id}/comment 鈥?鍙戣〃璇勮

> 浠呯鍩熸満鍣ㄤ汉鍙敤

**璇锋眰浣?*:

| 鍙傛暟 | 绫诲瀷 | 蹇呭～ | 璇存槑 |
|------|------|------|------|
| `thread_author` | string | 鏄?| 甯栧瓙浣滆€?ID |
| `content` | string | 鏄?| 璇勮鍐呭 |
| `thread_create_time` | string | 鍚?| 甯栧瓙鍒涘缓鏃堕棿 |
| `image` | string | 鍚?| 鍥剧墖閾炬帴 |

**杩斿洖**: `{ task_id: string, create_time: number }`

---

### POST /channels/{channel_id}/schedules 鈥?鍒涘缓鏃ョ▼

> 闇€瑕佺鐞嗛閬撴潈闄愩€傚崟绠＄悊鍛?澶╅檺 10 娆★紝鍗曢閬?澶╅檺 100 娆°€?
**璇锋眰浣?*:

```json
{
  "schedule": {
    "name": "鏃ョ▼鍚嶇О",
    "start_timestamp": "姣鏃堕棿鎴?,
    "end_timestamp": "姣鏃堕棿鎴?,
    "jump_channel_id": "0",
    "remind_type": "0"
  }
}
```

| 鍙傛暟 | 绫诲瀷 | 蹇呭～ | 璇存槑 |
|------|------|------|------|
| `schedule.name` | string | 鏄?| 鏃ョ▼鍚嶇О |
| `schedule.start_timestamp` | string | 鏄?| 寮€濮嬫椂闂达紙姣锛?|
| `schedule.end_timestamp` | string | 鏄?| 缁撴潫鏃堕棿锛堟绉掞級 |
| `schedule.jump_channel_id` | string | 鍚?| 璺宠浆瀛愰閬?ID锛岄粯璁?`"0"` |
| `schedule.remind_type` | string | 鍚?| 鎻愰啋绫诲瀷锛岄粯璁?`"0"` |

**杩斿洖**: `Schedule`

---

### PATCH /channels/{channel_id}/schedules/{schedule_id} 鈥?淇敼鏃ョ▼

> 闇€瑕佺鐞嗛閬撴潈闄?
**璇锋眰浣?*锛氬悓鍒涘缓鏃ョ▼

**杩斿洖**: `Schedule`

---

### DELETE /channels/{channel_id}/schedules/{schedule_id} 鈥?鍒犻櫎鏃ョ▼

> 鈿狅笍 涓嶅彲閫嗭紒闇€瑕佺鐞嗛閬撴潈闄?