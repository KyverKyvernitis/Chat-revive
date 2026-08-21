from __future__ import annotations


async def setup(bot):
    # Import tardio: mantém os helpers de estado testáveis sem carregar o
    # discord.py e evita trabalho de import quando a extensão não é usada.
    from .cog import AntibotCog

    await bot.add_cog(AntibotCog(bot))


__all__ = ["setup"]
