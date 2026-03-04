# Feishu (飞书) Channel Implementation Guide

## Overview

This document describes the Feishu (飞书) IM channel integration for Wegent.

## Implementation Details

### File Structure

```
backend/app/services/channels/feishu/
├── __init__.py              # Module exports
├── service.py               # FeishuChannelProvider - Main channel provider
├── handler.py               # FeishuChannelHandler - Message handler
├── callback.py              # FeishuCallbackService - Task completion callbacks
├── emitter.py               # StreamingResponseEmitter - Streaming updates
├── sender.py                # FeishuSender - Message sending utilities
└── user_resolver.py         # FeishuUserResolver - User mapping
```

### Channel Type Registration

The Feishu channel is registered as `ChannelType.FEISHU` with value `"feishu"`.

### Configuration

The Feishu channel uses the following configuration parameters:

```json
{
  "config": {
    "app_id": "your_app_id",
    "app_secret": "your_app_secret",
    "user_mapping_mode": "select_user|staff_id|email",
    "user_mapping_config": {
      "target_user_id": 1
    }
  }
}
```

### Webhook Setup

To receive messages from Feishu, you need to configure the Webhook endpoint:

1. Go to [Feishu Open Platform](https://open.feishu.cn)
2. Select your application
3. Go to "Event Subscription" or "Metadata Directory"
4. Configure the webhook URL: `{your_domain}/api/v1/im/feishu/webhook`
5. Set the verification token to match your configuration
6. Enable message events (`message.at`, `message`)

### API Endpoints

- **Webhook**: `/api/v1/im/feishu/webhook`
  - GET: Handles URL verification (returns `echostr`)
  - POST: Receives message events from Feishu

### Message Processing Flow

```
1. Feishu sends message to webhook endpoint
2. FeishuChannelProvider receives and validates event
3. WegentFeishuHandler processes the message:
   - Deduplicates using message_id
   - Resolves Feishu user to Wegent user
   - Updates IM binding for subscription notifications
4. Message is processed by the channel handler
5. Response is sent back via StreamingResponseEmitter
```

### User Mapping

The Feishu user resolver supports three mapping modes:

1. **select_user**: Map all Feishu users to a specific Wegent user
2. **staff_id**: Use Feishu's open_id as the username
3. **email**: Match users by email address

### Feishu SDK Integration

The implementation follows the [oapi-sdk-python](https://github.com/larksuite/oapi-sdk-python) pattern for:
- Access token management
- Event verification
- Message encryption/decryption
- API request signing

## Testing

To test the Feishu channel implementation:

```bash
# Run backend tests
cd backend
uv run pytest app/services/channels/feishu/

# Test imports
uv run python -c "from app.services.channels.feishu import *"
```

## Frontend Configuration

The IM Channel management UI supports Feishu configuration with:

- App ID field
- App Secret field (password type)
- User mapping mode selector
- Default team selection

## Comparison with DingTalk

| Feature | DingTalk | Feishu |
|---------|----------|--------|
| Authentication | client_id/client_secret | app_id/app_secret |
| Message Mode | Stream API | Webhook |
| Encryption | AES | AES |
| User Identifier | userId | open_id |

## Future Enhancements

Potential improvements:

1. Support for Feishu card messages for better UI
2. Group message handling
3. Message recall detection
4. File message support
5. Typing indicators
