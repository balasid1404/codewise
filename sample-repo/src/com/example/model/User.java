package com.example.model;

import java.util.UUID;

public class User {
    private String id;
    private String name;
    private String email;

    public User(String name, String email) {
        this.id = UUID.randomUUID().toString();
        this.name = name;
        this.email = email;
    }

    public String getId() {
        return id;
    }

    public String getName() {
        return name;
    }

    public String getEmail() {
        return email;
    }

    public User normalize() {
        this.name = name.trim().toLowerCase();
        this.email = email.trim().toLowerCase();
        return this;
    }
}
