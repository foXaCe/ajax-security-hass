# Ajax Security System Integration

Integration non-officielle pour Home Assistant permettant de contrôler votre système de sécurité Ajax.

## Fonctionnalités

- **Alarm Control Panel** : Contrôle du mode de sécurité en temps réel
  - Armement complet
  - Désarmement
  - Mode nuit
  - Synchronisation en temps réel via streaming gRPC

- **Capteurs** : Surveillance de l'état du système
  - Niveau de batterie des dispositifs
  - Température des dispositifs
  - État du hub

- **Boutons** : Actions rapides
  - Déclenchement du mode panique
  - Test de la sirène

## Installation

### Via HACS (recommandé)

1. Ouvrez HACS dans Home Assistant
2. Cliquez sur "Intégrations"
3. Cliquez sur les trois points en haut à droite
4. Sélectionnez "Dépôts personnalisés"
5. Ajoutez l'URL : `https://github.com/foXaCe/ajax-hass`
6. Sélectionnez la catégorie "Integration"
7. Cliquez sur "Télécharger"

### Installation manuelle

1. Téléchargez le fichier `ajax-hass.zip` depuis la [dernière release](https://github.com/foXaCe/ajax-hass/releases/latest)
2. Extrayez le dossier `ajax` dans votre répertoire `custom_components` de Home Assistant
3. Redémarrez Home Assistant

## Configuration

1. Allez dans Configuration > Intégrations
2. Cliquez sur "+ Ajouter une intégration"
3. Recherchez "Ajax Security System"
4. Entrez vos identifiants Ajax Systems (email et mot de passe)
5. Confirmez

## Prérequis

- Home Assistant 2023.8.0 ou plus récent
- Compte Ajax Systems valide

## Support

Pour signaler un problème ou demander une fonctionnalité, veuillez ouvrir une issue sur [GitHub](https://github.com/foXaCe/ajax-hass/issues).
