---
name: "n8n-pto-pipeline"
description: "Create n8n workflow for daily task assignment from PTO engineer to foreman via Telegram bot with status reporting."
homepage: "https://datadrivenconstruction.io"
metadata: {"openclaw": {"emoji": "🔧", "os": ["darwin", "linux", "win32"], "homepage": "https://datadrivenconstruction.io", "requires": {}}}
---
# n8n PTO-Foreman Pipeline

## Business Case

### Problem Statement
Daily work planning in construction involves:
- Manual task distribution from PTO (engineering) to field crews
- Paper-based or phone-based task assignment
- No systematic tracking of task completion
- Delayed reporting and status updates

### Solution
Automated n8n pipeline connecting Google Sheets task lists with Telegram bots for real-time task distribution and status collection.

### Business Value
- **Real-time distribution** - Tasks delivered automatically at 8:00 AM
- **Digital tracking** - All assignments and statuses in one table
- **Mobile-first** - Foremen use familiar Telegram interface
- **No app installation** - Works with any phone with Telegram

## Technical Implementation

### Architecture
```
┌─────────────────┐    ┌─────────────┐    ┌─────────────────┐
│  Google Sheets  │───>│  n8n        │───>│  Telegram Bot   │
│  (Task List)    │    │  Pipeline   │    │  (To Foreman)   │
└─────────────────┘    └─────────────┘    └─────────────────┘
        ▲                     │                    │
        │                     │                    ▼
        │              ┌──────┴──────┐      ┌───────────┐
        └──────────────│   Status    │<─────│  Foreman  │
                       │   Update    │      │  Response │
                       └─────────────┘      └───────────┘
```

### n8n Pipeline Components

#### 1. Morning Trigger (8:00 AM)
```json
{
  "nodes": [
    {
      "name": "Schedule Trigger",
      "type": "n8n-nodes-base.scheduleTrigger",
      "parameters": {
        "rule": {
          "interval": [
            {"field": "hours", "hoursInterval": 24}
          ]
        },
        "triggerTimes": {"item": [{"hour": 8, "minute": 0}]}
      }
    }
  ]
}
```

#### 2. Get Tasks from Google Sheets
```json
{
  "name": "Get Today Tasks",
  "type": "n8n-nodes-base.googleSheets",
  "parameters": {
    "operation": "read",
    "sheetId": "YOUR_SHEET_ID",
    "range": "Tasks!A:F",
    "options": {}
  }
}
```

#### 3. Filter Tasks by Foreman
```javascript
// Filter tasks for specific foreman based on chat_id
const chatId = $node["Telegram Trigger"].json["message"]["chat"]["id"];
const tasks = $input.all();

return tasks.filter(task =>
  task.json.foreman_chat_id === chatId.toString()
);
```

#### 4. Format and Send via Telegram
```javascript
// Format task message
const tasks = $input.all();
let message = "📋 *Задачи на сегодня:*\n\n";

tasks.forEach((task, index) => {
  message += `*${index + 1}. ${task.json.task_name}*\n`;
  message += `   📍 Участок: ${task.json.location}\n`;
  message += `   ⏰ Срок: ${task.json.deadline}\n`;
  message += `   📝 ${task.json.description}\n\n`;
});

message += "\n_Ответьте на это сообщение статусом:_\n";
message += "✅ выполнил\n❌ не выполнил + причина";

return [{json: {message}}];
```

#### 5. Status Update Handler
Runs after the Telegram Trigger, the `Get Today Tasks` read and a `Foremen` read of the Foremen sheet:
```json
{
  "name": "Foremen",
  "type": "n8n-nodes-base.googleSheets",
  "parameters": {
    "operation": "read",
    "sheetId": "YOUR_SHEET_ID",
    "range": "Foremen!A:C",
    "options": {}
  }
}
```

```javascript
// Parse foreman response and update status of the replied-to task
const incoming = $node["Telegram Trigger"].json["message"];
const replyTo = incoming && incoming.reply_to_message;
if (!replyTo || typeof incoming.text !== "string") return [];

// Telegram message ids are per-chat counters, so the replied-to id is only
// trusted for a task assigned to the sender's own chat.
const senderChatId = String(incoming.chat.id);
const isForeman = $("Foremen").all()
  .some(foreman => String(foreman.json.chat_id) === senderChatId);
if (!isForeman) return [];

const matches = $("Get Today Tasks").all()
  .filter(task => String(task.json.task_id) === String(replyTo.message_id));
if (matches.length !== 1) return [];
const task = matches[0].json;
if (String(task.foreman_chat_id) !== senderChatId) return [];

const message = incoming.text;
let status = "в работе";
let comment = "";

if (message.toLowerCase().includes("не выполнил")) {
  status = "не выполнено";
  comment = message.replace(/не выполнил/i, "").trim();
} else if (message.toLowerCase().includes("выполнил")) {
  status = "выполнено";
}

return [{
  json: {
    task_id: task.task_id,
    foreman_chat_id: task.foreman_chat_id,
    status: status,
    comment: comment,
    updated_at: new Date().toISOString()
  }
}];
```

Write the result to the Tasks sheet row matched on `task_id`. Replies from chats that are not on the Foremen sheet, or that are not the task's `foreman_chat_id`, produce no update.

### Google Sheets Structure

**Tasks Sheet:**
| Column | Description |
|--------|-------------|
| task_id | Unique task identifier |
| task_name | Task title |
| description | Detailed description |
| location | Work location |
| deadline | Due date/time |
| foreman_chat_id | Telegram chat ID of assigned foreman |
| status | Current status |
| comment | Foreman comment |

**Foremen Sheet:**
| Column | Description |
|--------|-------------|
| name | Foreman name |
| chat_id | Telegram chat ID |
| registered_at | Registration timestamp |

### Telegram Bot Setup

1. Create bot via @BotFather
2. Get bot token
3. Configure webhook in n8n
4. For local testing, use n8n tunnel:
```bash
npx n8n --tunnel
```

## Usage Flow

### For PTO Engineer:
1. Open Google Sheets task list
2. Add tasks with foreman assignments
3. System automatically sends at 8:00 AM

### For Foreman:
1. Receive tasks via Telegram bot
2. Reply to task message with status
3. System updates Google Sheets automatically (only for tasks assigned to that foreman's chat)

### For Manager:
1. View real-time status in Google Sheets
2. Generate reports from historical data
3. Analyze completion rates by foreman/location

## Deployment Options

### Local (Testing)
```bash
npx n8n --tunnel
```

### Cloud VPS (Production)
- Hostinger n8n: ~$5/month
- Amvera Cloud: ~170 RUB/month
- timeweb: ~590 RUB/month

## Extensions

- Add photo attachments for completed work
- Integrate with PostgreSQL for complex queries
- Add reminder notifications
- Generate daily/weekly reports
- Connect to project management systems

## Resources

- **Source**: DDC Telegram Community discussions
- **Template**: Available in DDC GitHub repository
