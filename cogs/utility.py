import discord
from discord import app_commands
from discord.ext import commands


class Utility(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="ping", description="Mostra o status atual e a latência do bot")
    async def ping(self, interaction: discord.Interaction):
        import os
        import time
        import psutil

        start = time.perf_counter()
        await interaction.response.defer(ephemeral=True)

        ws_ping = round(self.bot.latency * 1000)
        response_ping = round((time.perf_counter() - start) * 1000)

        now = discord.utils.utcnow()
        started_at = getattr(self.bot, "started_at", None)
        if started_at is None:
            uptime_text = "Desconhecido"
        else:
            delta = now - started_at
            total_seconds = int(delta.total_seconds())
            days, rem = divmod(total_seconds, 86400)
            hours, rem = divmod(rem, 3600)
            minutes, seconds = divmod(rem, 60)
            parts = []
            if days: parts.append(f"{days}d")
            if hours: parts.append(f"{hours}h")
            if minutes: parts.append(f"{minutes}m")
            if seconds or not parts: parts.append(f"{seconds}s")
            uptime_text = " ".join(parts)

        shard_id = getattr(interaction.guild, "shard_id", None)
        shard_text = str(shard_id) if shard_id is not None else "Único"
        db = getattr(self.bot, "settings_db", None)
        db_status = "🟢 Online" if db is not None else "🔴 Offline"

        process = psutil.Process(os.getpid())
        memory_mb = process.memory_info().rss / 1024 / 1024
        cpu_percent = psutil.cpu_percent(interval=0.2)

        if ws_ping < 120:
            status_text, color = "🟢 Excelente", discord.Color.green()
        elif ws_ping < 250:
            status_text, color = "🟡 Boa", discord.Color.gold()
        elif ws_ping < 400:
            status_text, color = "🟠 Instável", discord.Color.orange()
        else:
            status_text, color = "🔴 Alta", discord.Color.red()

        embed = discord.Embed(title="🏓 Pong!", description="Status atual do bot em tempo real.", color=color)
        embed.add_field(name="Latência WebSocket", value=f"`{ws_ping}ms`", inline=True)
        embed.add_field(name="Resposta do comando", value=f"`{response_ping}ms`", inline=True)
        embed.add_field(name="Status geral", value=status_text, inline=True)
        embed.add_field(name="Uptime", value=f"`{uptime_text}`", inline=True)
        embed.add_field(name="Banco de dados", value=db_status, inline=True)
        embed.add_field(name="Shard", value=f"`{shard_text}`", inline=True)
        embed.add_field(name="Uso de memória", value=f"`{memory_mb:.2f} MB`", inline=True)
        embed.add_field(name="Uso de CPU", value=f"`{cpu_percent:.1f}%`", inline=True)
        embed.add_field(name="Servidores", value=f"`{len(self.bot.guilds)}`", inline=True)
        if self.bot.user and self.bot.user.display_avatar:
            embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        embed.set_footer(text="Atualizado no momento do comando")
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Utility(bot))
