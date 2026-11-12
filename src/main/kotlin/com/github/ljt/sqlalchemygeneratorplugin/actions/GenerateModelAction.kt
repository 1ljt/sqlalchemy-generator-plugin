package com.github.ljt.sqlalchemygeneratorplugin.actions

import com.intellij.notification.NotificationGroupManager
import com.intellij.notification.NotificationType
import com.intellij.openapi.actionSystem.AnAction
import com.intellij.openapi.actionSystem.AnActionEvent

class GenerateModelAction : AnAction() {

    override fun actionPerformed(e: AnActionEvent) {
        val project = e.project ?: return
        val notification = NotificationGroupManager.getInstance()
            .getNotificationGroup("SQLAlchemy Generator")
        notification.createNotification(
            "SQLAlchemy Generator plugin is active. Connect to a database to generate ORM models.",
            NotificationType.INFORMATION
        ).notify(project)
    }
}
