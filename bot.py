import discord
from discord import app_commands
import os
import anthropic
import base64
import httpx
from datetime import datetime

# ── Credentials ──────────────────────────────────────────────────────────────
TOKEN          = os.environ['DISCORD_BOT_TOKEN']
ANTHROPIC_KEY  = os.environ['ANTHROPIC_API_KEY']
GITHUB_TOKEN   = os.environ['GITHUB_TOKEN']
GITHUB_REPO    = os.environ['GITHUB_REPO']
CHANNEL_LOG    = int(os.environ['DISCORD_CHANNEL_LOG'])

# ── Discord client ────────────────────────────────────────────────────────────
intents = discord.Intents.default()
client  = discord.Client(intents=intents)
tree    = app_commands.CommandTree(client)


# ── GitHub helpers ────────────────────────────────────────────────────────────
async def fetch_github_file(path: str) -> tuple[str, str]:
    """Returns (content, sha) for a file in the repo."""
    url = f'https://api.github.com/repos/{GITHUB_REPO}/contents/{path}'
    headers = {
        'Authorization': f'token {GITHUB_TOKEN}',
        'Accept': 'application/vnd.github.v3+json'
    }
    async with httpx.AsyncClient() as http:
        r = await http.get(url, headers=headers)
        r.raise_for_status()
        data = r.json()
        content = base64.b64decode(data['content']).decode('utf-8')
        return content, data['sha']


async def push_github_file(path: str, content: str, message: str) -> bool:
    """Push updated content to a file in the repo. Returns True on success."""
    url = f'https://api.github.com/repos/{GITHUB_REPO}/contents/{path}'
    headers = {
        'Authorization': f'token {GITHUB_TOKEN}',
        'Accept': 'application/vnd.github.v3+json'
    }
    async with httpx.AsyncClient() as http:
        # Get current SHA first
        r = await http.get(url, headers=headers)
        r.raise_for_status()
        sha = r.json()['sha']

        payload = {
            'message': message,
            'content': base64.b64encode(content.encode()).decode(),
            'sha': sha
        }
        r = await http.put(url, headers=headers, json=payload)
        return r.status_code in (200, 201)


# ── Claude helper ─────────────────────────────────────────────────────────────
async def generate_updated_context(answers: dict, current_context: str, about_me: str) -> str:
    """Ask Claude to regenerate current-context.md from weekly answers."""
    ac = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    prompt = f"""You are updating Andrew's current-context.md file for his Personal OS.

ABOUT ME (reference only):
{about_me}

PREVIOUS CURRENT-CONTEXT:
{current_context}

WEEKLY UPDATE ANSWERS:
- Cabana Cards status: {answers['cabana']}
- CHOMPO update: {answers['chompo']}
- RELICS update: {answers['relics']}
- Personal / family context: {answers['personal']}
- Top priorities this week: {answers['priorities']}
- Open loops / things weighing on me: {answers['open_loops']}

Generate a fresh current-context.md that:
1. Reflects the new information provided above
2. Keeps the same structure and format as the previous version
3. Updates the date to: {datetime.now().strftime('%B %Y')}
4. Keeps the "Notes for Claude" section accurate and honest
5. Is direct — no fluff, no padding

Return ONLY the raw markdown. No preamble, no code fences."""

    msg = ac.messages.create(
        model='claude-sonnet-4-20250514',
        max_tokens=2000,
        messages=[{'role': 'user', 'content': prompt}]
    )
    return msg.content[0].text


# ── Modal ─────────────────────────────────────────────────────────────────────
class WeeklyUpdateModal(discord.ui.Modal, title='Vega — Weekly Context Update'):

    cabana = discord.ui.TextInput(
        label='Cabana Cards — status & focus this week',
        placeholder='What\'s the state of play? What\'s the most important next action?',
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=500
    )
    chompo = discord.ui.TextInput(
        label='CHOMPO — any updates?',
        placeholder='Pivot decision progress? Anything active or decided?',
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=300
    )
    relics = discord.ui.TextInput(
        label='RELICS — any updates?',
        placeholder='Work consistency this week? Anything active or delivered?',
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=300
    )
    personal = discord.ui.TextInput(
        label='Personal / family — how are things?',
        placeholder='Energy level, health, family context, anything relevant.',
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=300
    )
    priorities = discord.ui.TextInput(
        label='Top priorities this week (3 max)',
        placeholder='The 3 things that matter most this week.',
        style=discord.TextStyle.short,
        required=True,
        max_length=300
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)

        log_ch = client.get_channel(CHANNEL_LOG)

        try:
            # Fetch current files from GitHub
            current_context, _ = await fetch_github_file('context/current-context.md')
            about_me, _         = await fetch_github_file('context/about-me.md')

            answers = {
                'cabana':     self.cabana.value,
                'chompo':     self.chompo.value    or 'No update.',
                'relics':     self.relics.value    or 'No update.',
                'personal':   self.personal.value  or 'No update.',
                'priorities': self.priorities.value,
                'open_loops': 'None specified.',   # surface in v2 if needed
            }

            # Generate updated context via Claude
            new_context = await generate_updated_context(answers, current_context, about_me)

            # Push to GitHub
            success = await push_github_file(
                'context/current-context.md',
                new_context,
                f'chore: weekly context update ({datetime.now().strftime("%Y-%m-%d")})'
            )

            if success:
                embed = discord.Embed(
                    title='✅ Context updated',
                    description='`current-context.md` regenerated and pushed to GitHub.',
                    color=0x57F287
                )
                embed.add_field(name='Cabana focus',     value=self.cabana.value[:100],     inline=False)
                embed.add_field(name='Priorities',       value=self.priorities.value[:100], inline=False)
                embed.set_footer(text=datetime.now().strftime('%b %d %Y %H:%M'))
                await interaction.followup.send(embed=embed)
                if log_ch:
                    await log_ch.send(f'📝 Context update pushed — {datetime.now().strftime("%b %d %Y")}')
            else:
                await interaction.followup.send('⚠️ Claude generated the update but the GitHub push failed. Check your GITHUB_TOKEN env var.')

        except Exception as e:
            await interaction.followup.send(f'❌ Something went wrong: `{e}`')
            if log_ch:
                await log_ch.send(f'❌ /update-context error: {e}')


# ── Slash commands ────────────────────────────────────────────────────────────
@tree.command(name='update-context', description='Run the weekly context update')
async def update_context(interaction: discord.Interaction):
    await interaction.response.send_modal(WeeklyUpdateModal())


@tree.command(name='status', description='Quick status snapshot across all ventures')
async def status(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)

    try:
        context, _ = await fetch_github_file('context/current-context.md')

        ac = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        msg = ac.messages.create(
            model='claude-sonnet-4-20250514',
            max_tokens=400,
            messages=[{
                'role': 'user',
                'content': (
                    'From this current-context doc, give a tight status summary. '
                    'Format exactly like this:\n\n'
                    '**Cabana** — one sentence\n'
                    '**CHOMPO** — one sentence\n'
                    '**RELICS** — one sentence\n\n'
                    '**This week:** one sentence on the top priority\n\n'
                    'No fluff. No intro. Just those four lines.\n\n'
                    f'{context}'
                )
            }]
        )

        embed = discord.Embed(
            title='📊 Status',
            description=msg.content[0].text,
            color=0x5865F2
        )
        embed.set_footer(text=f'current-context.md • {datetime.now().strftime("%b %d %Y")}')
        await interaction.followup.send(embed=embed)

    except Exception as e:
        await interaction.followup.send(f'❌ Error fetching status: `{e}`')


@tree.command(name='help', description='List all Vega commands')
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(
        title='Vega — Commands',
        color=0xFEE75C
    )
    embed.add_field(
        name='/update-context',
        value='Opens a form to update your weekly current-context.md. Takes ~2 min. Pushes to GitHub automatically.',
        inline=False
    )
    embed.add_field(
        name='/status',
        value='Quick one-line status across Cabana, CHOMPO, and RELICS from your current context.',
        inline=False
    )
    embed.add_field(
        name='/help',
        value='This message.',
        inline=False
    )
    await interaction.response.send_message(embed=embed)


# ── Startup ───────────────────────────────────────────────────────────────────
@client.event
async def on_ready():
    await tree.sync()
    print(f'Vega online as {client.user}')
    log_ch = client.get_channel(CHANNEL_LOG)
    if log_ch:
        await log_ch.send('🟢 Vega is online.')


client.run(TOKEN)
