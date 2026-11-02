import os
import requests

token = '8921341725:AAFRPRj9xxPIuEyNLTR8Iob0TtIC54n1hfE'
r = requests.get(f'https://api.telegram.org/bot{token}/getUpdates')
data = r.json()

found = False
for update in data.get('result', []):
    if 'my_chat_member' in update:
        chat = update['my_chat_member']['chat']
        print(f"Channel/Group: {chat.get('title')} | ID: {chat.get('id')}")
        found = True
    elif 'message' in update and 'chat' in update['message']:
        chat = update['message']['chat']
        if str(chat.get('id')).startswith('-100'):
            print(f"Message in Channel/Group: {chat.get('title')} | ID: {chat.get('id')}")
            found = True
    elif 'channel_post' in update:
        chat = update['channel_post']['chat']
        print(f"Channel Post in: {chat.get('title')} | ID: {chat.get('id')}")
        found = True

if not found:
    print('No channel IDs found in getUpdates.')
