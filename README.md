# Haru — bot de modération + tickets

## Installation

1. `npm install`
2. Copie `.env.example` en `.env` et mets ton token : `DISCORD_TOKEN=...`
3. Dans le [portail développeur Discord](https://discord.com/developers/applications) → ton bot → **Bot**, active **Server Members Intent** et **Message Content Intent**.
4. Invite le bot avec la permission **Administrateur** (ou au minimum : Gérer les salons, Gérer les rôles, Bannir, Exclure des membres, Voir les salons, Envoyer des messages, Intégrer des liens).
5. `npm start`

> Place le rôle du bot **au-dessus** des rôles staff et des membres à sanctionner.

## Commandes (préfixe `+`)

| Commande | Niveau | Description |
|---|---|---|
| `+warn @membre [raison]` | Helper | Avertit un membre (enregistré dans `data/warns.json`) |
| `+mute @membre [durée] [raison]` | Modérateur | Timeout Discord (ex. `10m`, `2h`, `1d`, 28j max / par défaut) |
| `+unmute @membre` | Modérateur | Retire le mute |
| `+unwarn @membre [numéro]` | Modérateur | Retire un warn (le dernier si pas de numéro) |
| `+lock [#salon] [raison]` | Chef Modérateur | Empêche @everyone d'écrire |
| `+unlock [#salon]` | Chef Modérateur | Réactive l'écriture |
| `+ban @membre\|ID [raison]` | Responsable Staff | Bannit (même hors serveur avec l'ID) |
| `+unban ID [raison]` | Responsable Staff | Débannit |
| `+ticketmenu` | Responsable Staff | Poste le menu « Tickets Haru » dans le salon |

Les niveaux supérieurs héritent des commandes des niveaux inférieurs, et les administrateurs ont tous les droits.
Tout se règle dans `src/config.js` (noms des rôles, niveaux par commande, catégories de tickets, salon de logs).

## Tickets

`+ticketmenu` envoie l'embed **Tickets Haru** avec le menu « Choisir une catégorie... ».
Choisir une catégorie crée un salon privé (visible par le membre et le staff) dans la catégorie `Tickets` (créée automatiquement), avec un bouton **Fermer le ticket**. Un seul ticket ouvert par membre.

## Logs

Si un salon nommé `logs-modération` existe, chaque sanction y est loguée.
