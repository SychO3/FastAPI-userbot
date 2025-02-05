import os
import io
from fastapi import FastAPI, HTTPException, Form, Depends, UploadFile, File, Query
from pyrogram import Client, types
from pyrogram.enums import ChatMembersFilter
from pyrogram.types import ChatPrivileges
from pyrogram.errors import (
    PeerIdInvalid,
    ChatAdminRequired,
    UserNotParticipant,
    InviteRequestSent,
)
from contextlib import asynccontextmanager
from fastapi.security import HTTPAuthorizationCredentials
from redis import asyncio as aioredis
import json

import logging

logging.basicConfig(level=logging.INFO)

from models import (
    CreateSupergroupRequest,
    AddChatMembersRequest,
    BanChatMemberRequest,
    SendMessageRequest,
    AddContactRequest,
    PromoteChatMemberRequest,
    GetChatMembersRequest,
    JoinChatRequest,
    LeaveChatRequest,
)
from errors import UserNotFoundError, GroupNotFoundError, UsernameNotOccupied
from auth import authenticate
from dotenv import load_dotenv

### Session setup ###
load_dotenv()

API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
PHONE_NUMBER = os.getenv("PHONE_NUMBER")
SESSION_NAME = f"listener_{PHONE_NUMBER.replace('+', '')}"
SESSION_FILE = f"{SESSION_NAME}.session"
TOKEN = os.getenv("SECRET_TOKEN")
MAIN_REDIS_KEY = f"listener:{PHONE_NUMBER.replace('+', '')}:"

redis_client = aioredis.from_url(os.getenv("REDIS_URL"), decode_responses=True)


pyro_client: Client = None


async def handle_message(client: Client, message: types.Message):
    try:
        if not message.from_user:
            return

        if message.from_user.is_self:
            return

        text = message.text or message.caption

        if not text:
            return

        if message.chat.type.value == "private":
            return

        if message.from_user.is_bot:
            return

        full_name = message.from_user.full_name
        username = message.from_user.username
        user_id = message.from_user.id

        logging.info(
            f"Received message from {username} [{user_id}] ({full_name}): {text:.50}"
        )

        key = "listener:keywords"
        keywords = await redis_client.get(key)

        if not keywords:
            return

        keywords = json.loads(keywords)

        is_push = False

        for keyword in keywords:
            is_active = keyword.get("is_active", False)
            if not is_active:
                continue

            # 获取关键词匹配参数
            match_pattern = keyword.get("match_pattern", "exact")
            word_limit = keyword.get("word_limit", 0)
            has_username = keyword.get("has_username", 0)
            target_keyword = keyword.get("keyword", "")
            user_id = keyword.get("user_id")

            # 根据匹配模式进行匹配
            message_text = message.text or message.caption or ""
            if match_pattern == "exact" and target_keyword in message_text:
                is_push = True
            elif (
                match_pattern == "fuzzy"
                and target_keyword.lower() in message_text.lower()
            ):
                is_push = True

            # 如果需要检查用户名且消息没有用户名，则跳过
            if has_username and not message.from_user.username:
                continue

            # 如果有字数限制且不满足，则跳过
            if word_limit > 0 and len(message_text.split()) < word_limit:
                continue

            # 如果匹配成功，保存到Redis
            if is_push:
                push_data = {
                    "message_id": message.id,
                    "message_link": message.link,
                    "chat_title": message.chat.title,
                    "chat_username": message.chat.username,
                    "chat_type": message.chat.type.value,
                    "chat_id": message.chat.id,
                    "user_name": message.from_user.username,
                    "user_full_name": message.from_user.full_name,
                    "user_id": message.from_user.id,
                    "text": message_text,
                    "date": message.date.timestamp(),
                    "matched_keyword": target_keyword,
                }

                # 使用Redis列表存储待推送消息
                push_key = f"listener:push:messages:{user_id}"
                await redis_client.rpush(push_key, json.dumps(push_data))

                # 设置过期时间 24小时
                await redis_client.expire(push_key, 24 * 60 * 60)

                logging.info(f"Pushed message to user {user_id}")

                break  # 匹配成功一次后就退出循环

    except Exception as e:
        logging.error(f"Error handling message: {str(e)}")


@asynccontextmanager
async def lifespan(application: FastAPI):
    global pyro_client
    pyro_client = Client(
        SESSION_NAME,
        api_id=API_ID,
        api_hash=API_HASH,
        phone_number=PHONE_NUMBER,
        workdir=os.getcwd(),
    )
    pyro_client.on_message()(handle_message)
    await pyro_client.start()
    yield
    await pyro_client.stop()


app = FastAPI(title="FastAPI Telegram Group Manager Backend", lifespan=lifespan)


### Endpoints ###


@app.post("/create_supergroup")
async def create_supergroup(
    request: CreateSupergroupRequest,
    credentials: HTTPAuthorizationCredentials = Depends(authenticate),
):
    try:
        chat = await pyro_client.create_supergroup(request.title, request.description)
        return {
            "groupid": chat.id,
            "title": chat.title,
            "description": request.description,
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/add_chat_members")
async def add_chat_members(
    request: AddChatMembersRequest,
    credentials: HTTPAuthorizationCredentials = Depends(authenticate),
):
    try:
        await pyro_client.add_chat_members(request.group_id, request.user_ids)
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/ban_chat_member")
async def ban_chat_member(
    request: BanChatMemberRequest,
    credentials: HTTPAuthorizationCredentials = Depends(authenticate),
):
    try:
        await pyro_client.ban_chat_member(request.chat_id, request.user_id)
        return {"status": "success"}
    except ChatAdminRequired:
        raise PermissionError("You need to be an admin to ban users.")
    except UserNotParticipant:
        raise UserNotFoundError(request.user_id)
    except PeerIdInvalid:
        raise GroupNotFoundError(request.chat_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/send_message")
async def send_message(
    request: SendMessageRequest,
    credentials: HTTPAuthorizationCredentials = Depends(authenticate),
):
    try:
        message = await pyro_client.send_message(request.user_id, request.text)
        return {"message_id": message.id, "status": "success"}
    except UserNotParticipant:
        raise UserNotFoundError(request.user_id)
    except PeerIdInvalid:
        raise HTTPException(status_code=400, detail="Invalid user ID.")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/add_contact")
async def add_contact(
    request: AddContactRequest,
    credentials: HTTPAuthorizationCredentials = Depends(authenticate),
):
    try:
        user = await pyro_client.add_contact(
            request.user_id, request.first_name, request.last_name
        )
        return {"user_id": user.id, "status": "success"}
    except UsernameNotOccupied:
        raise HTTPException(status_code=400, detail="Invalid username.")
    except PeerIdInvalid:
        raise HTTPException(status_code=400, detail="Invalid user ID.")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/promote_chat_member")
async def promote_chat_member(
    request: PromoteChatMemberRequest,
    credentials: HTTPAuthorizationCredentials = Depends(authenticate),
):
    try:
        privileges = ChatPrivileges(
            can_manage_chat=request.can_manage_chat,
            can_delete_messages=request.can_delete_messages,
            can_delete_stories=request.can_delete_stories,
            can_manage_video_chats=request.can_manage_video_chats,
            can_restrict_members=request.can_restrict_members,
            can_promote_members=request.can_promote_members,
            can_change_info=request.can_change_info,
            can_post_messages=request.can_post_messages,
            can_post_stories=request.can_post_stories,
            can_edit_messages=request.can_edit_messages,
            can_edit_stories=request.can_edit_stories,
            can_invite_users=request.can_invite_users,
            can_pin_messages=request.can_pin_messages,
            can_manage_topics=request.can_manage_topics,
            is_anonymous=request.is_anonymous,
        )

        success = await pyro_client.promote_chat_member(
            request.chat_id, request.user_id, privileges=privileges
        )

        return {"status": "success" if success else "failed"}
    except ChatAdminRequired:
        raise PermissionError("You need to be an admin to promote members.")
    except UserNotParticipant:
        raise UserNotFoundError(request.user_id)
    except PeerIdInvalid:
        raise GroupNotFoundError(request.chat_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/get_chat_members")
async def get_chat_members(
    request: GetChatMembersRequest,
    credentials: HTTPAuthorizationCredentials = Depends(authenticate),
):
    try:
        members = []

        # Map the string filter to the actual ChatMembersFilter enum
        filter_mapping = {
            "search": ChatMembersFilter.SEARCH,
            "administrators": ChatMembersFilter.ADMINISTRATORS,
            "restricted": ChatMembersFilter.RESTRICTED,
            "banned": ChatMembersFilter.BANNED,
            "bots": ChatMembersFilter.BOTS,
            "recent": ChatMembersFilter.RECENT,
        }

        # Default to SEARCH filter if not provided or if it's an empty string
        chat_filter = filter_mapping.get(request.filter, ChatMembersFilter.SEARCH)

        async for member in pyro_client.get_chat_members(
            request.chat_id, limit=request.limit or 1000, filter=chat_filter
        ):
            member_dict = {
                "user_id": member.user.id,
                "user_name": member.user.username,
                "status": member.status,
                "is_bot": member.user.is_bot,
                "chat": member.chat.title if member.chat else None,
                "joined_date": member.joined_date.isoformat()
                if member.joined_date
                else None,
                "custom_title": member.custom_title,
                "until_date": member.until_date.isoformat()
                if member.until_date
                else None,
                "invited_by": member.invited_by.username if member.invited_by else None,
                "promoted_by": member.promoted_by.username
                if member.promoted_by
                else None,
                "restricted_by": member.restricted_by.username
                if member.restricted_by
                else None,
                "is_member": member.is_member,
                "can_be_edited": member.can_be_edited,
                "subscription_until_date": member.subscription_until_date
                if member.subscription_until_date
                else None,
                "permissions": {
                    "can_send_messages": member.permissions.can_send_messages
                    if member.permissions
                    else None,
                    "can_send_media_messages": member.permissions.can_send_media_messages
                    if member.permissions
                    else None,
                    "can_send_polls": member.permissions.can_send_polls
                    if member.permissions
                    else None,
                    "can_add_web_page_previews": member.permissions.can_add_web_page_previews
                    if member.permissions
                    else None,
                    "can_change_info": member.permissions.can_change_info
                    if member.permissions
                    else None,
                    "can_invite_users": member.permissions.can_invite_users
                    if member.permissions
                    else None,
                    "can_pin_messages": member.permissions.can_pin_messages
                    if member.permissions
                    else None,
                    "can_manage_topics": member.permissions.can_manage_topics
                    if member.permissions
                    else None,
                    "can_send_audios": member.permissions.can_send_audios
                    if member.permissions
                    else None,
                    "can_send_docs": member.permissions.can_send_docs
                    if member.permissions
                    else None,
                    "can_send_games": member.permissions.can_send_games
                    if member.permissions
                    else None,
                    "can_send_gifs": member.permissions.can_send_gifs
                    if member.permissions
                    else None,
                    "can_send_inline": member.permissions.can_send_inline
                    if member.permissions
                    else None,
                    "can_send_photos": member.permissions.can_send_photos
                    if member.permissions
                    else None,
                    "can_send_plain": member.permissions.can_send_plain
                    if member.permissions
                    else None,
                    "can_send_roundvideos": member.permissions.can_send_roundvideos
                    if member.permissions
                    else None,
                    "can_send_stickers": member.permissions.can_send_stickers
                    if member.permissions
                    else None,
                    "can_send_videos": member.permissions.can_send_videos
                    if member.permissions
                    else None,
                    "can_send_voice": member.permissions.can_send_voices
                    if member.permissions
                    else None,
                }
                if member.permissions
                else None,
                "privileges": {
                    "can_manage_chat": member.privileges.can_manage_chat,
                    "can_delete_messages": member.privileges.can_delete_messages,
                    "can_delete_stories": member.privileges.can_delete_stories,
                    "can_manage_video_chats": member.privileges.can_manage_video_chats,
                    "can_restrict_members": member.privileges.can_restrict_members,
                    "can_promote_members": member.privileges.can_promote_members,
                    "can_change_info": member.privileges.can_change_info,
                    "can_post_messages": member.privileges.can_post_messages,
                    "can_edit_messages": member.privileges.can_edit_messages,
                    "can_edit_stories": member.privileges.can_edit_stories,
                    "can_invite_users": member.privileges.can_invite_users,
                    "can_pin_messages": member.privileges.can_pin_messages,
                    "can_manage_topics": member.privileges.can_manage_topics,
                    "is_anonymous": member.privileges.is_anonymous,
                }
                if member.privileges
                else None,
            }
            members.append(member_dict)

        return {"members": members}
    except ChatAdminRequired:
        raise HTTPException(
            status_code=403, detail="You need to be an admin to get the members list."
        )
    except UserNotParticipant:
        raise HTTPException(
            status_code=404, detail="The user is not a participant in the chat."
        )
    except PeerIdInvalid:
        raise HTTPException(status_code=400, detail="Invalid chat ID.")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/set_chat_photo")
async def set_chat_photo(
    chat_id: int = Form(...),
    file: UploadFile = File(...),
    credentials: HTTPAuthorizationCredentials = Depends(authenticate),
):
    try:
        if file.file is None:
            raise HTTPException(status_code=400, detail="No file uploaded")

        if file.content_type.startswith("image"):
            file_content = await file.read()
            file_like_object = io.BytesIO(file_content)

            # Directly pass the BytesIO object to Pyrogram
            await pyro_client.set_chat_photo(chat_id, photo=file_like_object)

            # Cleanup
            file_like_object.close()

        else:
            raise HTTPException(
                status_code=415,
                detail="Unsupported Media Type. Please upload an image or video file.",
            )
        return {"status": "success"}
    except ChatAdminRequired:
        raise PermissionError("You need to be an admin to change the chat photo.")
    except PeerIdInvalid:
        raise GroupNotFoundError(chat_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/get_dialogs")
async def get_dialogs(
    limit: int = Form(None),
    credentials: HTTPAuthorizationCredentials = Depends(authenticate),
):
    try:
        dialogs = []
        async for dialog in pyro_client.get_dialogs(limit=limit):
            dialogs.append(
                {
                    "id": dialog.chat.id,
                    "title": dialog.chat.title
                    if dialog.chat.title
                    else dialog.chat.first_name,
                    "type": dialog.chat.type.value,
                    "username": dialog.chat.username if dialog.chat.username else None,
                    "creator": dialog.chat.is_creator,
                }
            )
        return {"dialogs": dialogs}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/get_me")
async def get_me(token: str = Query(...)):
    try:
        if token != TOKEN:
            raise HTTPException(status_code=401, detail="Invalid token")
        me = await pyro_client.get_me()
        return {
            "id": me.id,
            "username": me.username,
            "full_name": me.full_name,
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/join_chat")
async def join_chat(
    request: JoinChatRequest,
    credentials: HTTPAuthorizationCredentials = Depends(authenticate),
):
    try:
        chat = await pyro_client.join_chat(request.chat_id)
        return {"status": "success", "message": f"Successfully joined {chat.title}"}
    except PeerIdInvalid:
        raise HTTPException(status_code=404, detail="Chat not found")
    except InviteRequestSent:
        return {"status": "verify", "message": "Invite request sent"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/leave_chat")
async def leave_chat(
    request: LeaveChatRequest,
    credentials: HTTPAuthorizationCredentials = Depends(authenticate),
):
    try:
        await pyro_client.leave_chat(request.chat_id)
        return {"status": "success", "message": "Successfully left the chat"}
    except PeerIdInvalid:
        raise HTTPException(status_code=404, detail="Chat not found")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
