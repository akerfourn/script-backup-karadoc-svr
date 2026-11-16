"""Build the small PySide form separately from the backup lifecycle."""

from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class BackupForm(QWidget):
    """Connection settings, explicit mirror actions and the live journal."""

    def __init__(self) -> None:
        super().__init__()
        self.host = QLineEdit("kerfadmin@karadoc.kerfourn.net")
        self.destination = QLineEdit("/media/adrien/backup")
        self.ssh_password = self.password_field()
        self.sudo_password = self.password_field()
        self.same_password = QCheckBox("Même mot de passe pour SSH et sudo sur le serveur")
        self.same_password.setChecked(True)
        self.remote_sudo = QCheckBox("Utiliser sudo sur le serveur")
        self.remote_sudo.setChecked(True)
        self.home_only = QCheckBox("Copier uniquement /home")
        self.prepare = QCheckBox("Rafraîchir le dump et la configuration Nextcloud avant la copie")
        self.simulate = QPushButton("Simuler")
        self.synchronize = QPushButton("Synchroniser le miroir")
        self.stop = QPushButton("Arrêter")
        self.stop.setEnabled(False)
        self.status = QLabel("Prêt. Commencer par une simulation.")
        self.journal = QPlainTextEdit()
        self.journal.setReadOnly(True)
        self.journal.setMaximumBlockCount(4000)
        self.build_layout()
        self.same_password.toggled.connect(self.update_sudo_fields)
        self.remote_sudo.toggled.connect(self.update_sudo_fields)
        self.home_only.toggled.connect(self.update_prepare_field)
        self.update_sudo_fields()

    @staticmethod
    def password_field() -> QLineEdit:
        """Create a masked field whose content is never persisted."""
        field = QLineEdit()
        field.setEchoMode(QLineEdit.EchoMode.Password)
        return field

    def build_layout(self) -> None:
        """Keep destination selection next to its mount path."""
        browse = QPushButton("Parcourir…")
        browse.clicked.connect(self.choose_destination)
        destination_row = QHBoxLayout()
        destination_row.addWidget(self.destination)
        destination_row.addWidget(browse)
        self.settings_widget = QWidget()
        fields = QFormLayout(self.settings_widget)
        fields.addRow("Serveur ou alias SSH", self.host)
        fields.addRow("Disque déjà monté", destination_row)
        fields.addRow("Mot de passe SSH", self.ssh_password)
        fields.addRow("", self.remote_sudo)
        fields.addRow("", self.same_password)
        fields.addRow("Mot de passe sudo distant", self.sudo_password)
        fields.addRow("", self.home_only)
        fields.addRow("", self.prepare)
        actions = QHBoxLayout()
        actions.addWidget(self.simulate)
        actions.addWidget(self.synchronize)
        actions.addStretch()
        actions.addWidget(self.stop)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Le disque conserve home/, nextcloud/ et config/.\n"
                                "La synchronisation supprime les fichiers absents du serveur."))
        layout.addWidget(self.settings_widget)
        layout.addLayout(actions)
        layout.addWidget(self.status)
        layout.addWidget(self.journal)

    def choose_destination(self) -> None:
        """Select the already mounted disk without mounting anything."""
        directory = QFileDialog.getExistingDirectory(self, "Point de montage du disque", self.destination.text())
        if directory != "":
            self.destination.setText(directory)

    def update_sudo_fields(self) -> None:
        """Ask for a second remote password only when explicitly selected."""
        self.same_password.setEnabled(self.remote_sudo.isChecked())
        self.sudo_password.setEnabled(self.remote_sudo.isChecked() and not self.same_password.isChecked())

    def update_prepare_field(self) -> None:
        """The home-only operation does not prepare Nextcloud exports."""
        self.prepare.setEnabled(not self.home_only.isChecked())

    def set_busy(self, is_busy: bool) -> None:
        """Prevent concurrent operations and edits during an active transfer."""
        self.settings_widget.setEnabled(not is_busy)
        self.simulate.setEnabled(not is_busy)
        self.synchronize.setEnabled(not is_busy)
        self.stop.setEnabled(is_busy)
